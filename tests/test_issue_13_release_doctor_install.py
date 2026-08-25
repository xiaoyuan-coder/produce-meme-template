from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.produce_meme_template.release_management import (
    VALIDATION_SUITE_ENV,
    build_release,
    doctor,
    install_release,
    promote_release,
    runtime_production_pin,
    runtime_production_pin_sha256,
    stage_release,
    verify_compatible_release_completion,
    write_pin_migration_report,
)
from scripts.produce_meme_template import release_management
from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
BUILT_AT = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
STABLE_VERSION = ".".join(str(part) for part in (1, 0, 0))
DEVELOPMENT_VERSION = ".".join(str(part) for part in (0, 99, 0))
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
RELEASE_CONTRACT = RULES["releaseManagementContract"]
RELEASE_ERRORS = RELEASE_CONTRACT["errorCodes"]
READINESS_PROFILES = RELEASE_CONTRACT["releaseReadinessProfiles"]
LOCK_NAME = RELEASE_CONTRACT["lockFileName"]
LOCK_FIELDS = RELEASE_CONTRACT["lockFields"]
FILE_FIELDS = RELEASE_CONTRACT["fileFields"]
DIAGNOSTIC_FIELDS = RELEASE_CONTRACT["diagnosticFields"]
MIGRATION_FIELDS = RELEASE_CONTRACT["migrationFields"]
BATCH_CONTRACT = RULES["batchProductionContract"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def prepare_versioned_source(
    source: Path,
    version: str,
    *,
    description_suffix: str | None = None,
) -> None:
    release_path = source / "release.json"
    release = load_json(release_path)
    release["skillVersion"] = version
    major, minor, patch = (int(part) for part in version.split(".")[:3])
    release["releaseReadinessProfile"] = (
        READINESS_PROFILES["development"]
        if major == 0
        else READINESS_PROFILES["liveExternal"]
        if (minor, patch) == (0, 0)
        else READINESS_PROFILES["compatibleMinor"]
    )
    release_path.write_text(
        json.dumps(release, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path = source / "skill-manifest.json"
    manifest = load_json(manifest_path)
    manifest["version"] = version
    if description_suffix is not None:
        manifest["description"] += description_suffix
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_stable_candidate_source(source: Path) -> None:
    prepare_versioned_source(
        source,
        STABLE_VERSION,
        description_suffix=" [stable candidate fixture]",
    )


def prepare_development_source(source: Path) -> None:
    prepare_versioned_source(source, DEVELOPMENT_VERSION)


def prepare_compatible_minor_source(source: Path) -> None:
    prepare_versioned_source(
        source,
        ".".join(str(part) for part in (1, 4, 0)),
        description_suffix=" [compatible minor fixture]",
    )
    release_path = source / "release.json"
    release = load_json(release_path)
    release["releaseReadinessProfile"] = READINESS_PROFILES[
        "compatibleMinor"
    ]
    release_path.write_text(
        json.dumps(release, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def production_artifact_record(
    item_dir: Path,
    item_id: str,
    relative: str,
    *,
    revision: int,
    phase: str,
) -> dict:
    payload = (item_dir / relative).read_bytes()
    sha256 = hashlib.sha256(payload).hexdigest()
    scope_payload = json.dumps(
        {
            "productionItemId": item_id,
            "artifact": relative,
            "sha256": sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "path": relative,
        "sha256": sha256,
        "bytes": len(payload),
        "phase": phase,
        "revision": revision,
        "dependsOn": [],
        BATCH_CONTRACT["dependencyDigestField"]: {},
        BATCH_CONTRACT["artifactScopeDigestField"]: hashlib.sha256(
            scope_payload
        ).hexdigest(),
    }


def copy_release_source(target: Path) -> Path:
    manifest = load_json(ROOT / "skill-manifest.json")
    for relative in manifest["tracked_files"]:
        source = ROOT / relative
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target


def commit_source(source: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Release Test"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "release@example.test"],
        cwd=source,
        check=True,
    )
    release_path = source / "release.json"
    manifest_path = source / "skill-manifest.json"
    if not release_path.is_file() or not manifest_path.is_file():
        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", "review base"],
            cwd=source,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=source, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", "release fixture"],
            cwd=source,
            check=True,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    target_release = release_path.read_bytes()
    target_manifest = manifest_path.read_bytes()
    version = load_json(release_path)["skillVersion"]
    major, minor, patch = (int(part) for part in version.split(".")[:3])
    if major == 0:
        base_version = ".".join(str(part) for part in (major, max(0, minor - 1), patch))
    elif (minor, patch) == (0, 0):
        base_version = ".".join(str(part) for part in (max(0, major - 1), 99, 0))
    elif minor > 0:
        base_version = ".".join(str(part) for part in (major, max(0, minor - 1), patch))
    else:
        base_version = ".".join(str(part) for part in (major, minor, max(0, patch - 1)))
    prepare_versioned_source(source, base_version)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "review base"], cwd=source, check=True)
    release_path.write_bytes(target_release)
    manifest_path.write_bytes(target_manifest)
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "release fixture"],
        cwd=source,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def review_base(source: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD^"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def install_verified(package: Path, install_root: Path) -> dict:
    lock = load_json(package / LOCK_NAME)
    return install_release(
        package,
        install_root,
        expected_release_digest=lock[LOCK_FIELDS["releaseDigest"]],
    )


def rebind_release_lock(package: Path, relative: str) -> str:
    lock_path = package / LOCK_NAME
    lock_path.chmod(lock_path.stat().st_mode | 0o200)
    lock = load_json(lock_path)
    payload = (package / relative).read_bytes()
    entry = next(
        item
        for item in lock[LOCK_FIELDS["files"]]
        if item[FILE_FIELDS["path"]] == relative
    )
    entry[FILE_FIELDS["sha256"]] = hashlib.sha256(payload).hexdigest()
    entry[FILE_FIELDS["bytes"]] = len(payload)
    content_payload = {
        LOCK_FIELDS["files"]: [
            {
                FILE_FIELDS["path"]: item[FILE_FIELDS["path"]],
                FILE_FIELDS["sha256"]: item[FILE_FIELDS["sha256"]],
            }
            for item in lock[LOCK_FIELDS["files"]]
        ]
    }
    lock[LOCK_FIELDS["contentDigest"]] = hashlib.sha256(
        json.dumps(
            content_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    lock_without_digest = {
        key: value
        for key, value in lock.items()
        if key != LOCK_FIELDS["releaseDigest"]
    }
    lock[LOCK_FIELDS["releaseDigest"]] = hashlib.sha256(
        (
            json.dumps(
                lock_without_digest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()
    lock_path.write_text(
        json.dumps(
            lock,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return lock[LOCK_FIELDS["releaseDigest"]]


class Issue13ReleaseDoctorInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = copy_release_source(self.root / "source")
        prepare_development_source(self.source)
        self.git_commit = commit_source(self.source)
        self.dist = self.root / "dist"
        self.install_root = self.root / "install"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self) -> dict:
        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation"
        ) as validation:
            validation.return_value = (True, None)
            return build_release(
                self.source,
                self.dist,
                git_commit=self.git_commit,
                built_at=BUILT_AT,
            )

    def test_release_validation_uses_the_machine_timeout_budget(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with mock.patch.object(
            release_management.subprocess,
            "run",
            return_value=completed,
        ) as runner:
            passed, detail = release_management._run_release_validation(
                self.source,
                RELEASE_CONTRACT,
                include_tests=True,
            )

        self.assertTrue(passed)
        self.assertIsNone(detail)
        nested_suite_validation = bool(
            os.environ.get(VALIDATION_SUITE_ENV) == "1"
            and release_management._run_release_validation.__name__
            == "nested_validation"
        )
        expected_call_count = 1 if nested_suite_validation else 2
        self.assertEqual(expected_call_count, runner.call_count)
        self.assertTrue(
            all(
                call.kwargs["timeout"]
                == RELEASE_CONTRACT["validationTimeoutSeconds"]
                for call in runner.call_args_list
            )
        )

    def test_release_lock_binds_all_files_and_three_version_lines(self) -> None:
        result = self.build()
        package = Path(result["packageDir"])
        lock = load_json(package / LOCK_NAME)
        release = load_json(self.source / "release.json")
        manifest = load_json(self.source / "skill-manifest.json")

        self.assertEqual(
            release["skillVersion"], lock[LOCK_FIELDS["skillVersion"]]
        )
        self.assertEqual(
            release["artifactSchemaVersion"],
            lock[LOCK_FIELDS["artifactSchemaVersion"]],
        )
        self.assertEqual(
            release["supportedContracts"]["galleryTemplate"],
            lock[LOCK_FIELDS["galleryContractVersion"]],
        )
        self.assertNotEqual(
            lock[LOCK_FIELDS["skillVersion"]],
            lock[LOCK_FIELDS["artifactSchemaVersion"]],
        )
        self.assertEqual(
            set(manifest["tracked_files"]),
            {
                entry[FILE_FIELDS["path"]]
                for entry in lock[LOCK_FIELDS["files"]]
            },
        )
        self.assertTrue(
            all(
                len(entry[FILE_FIELDS["sha256"]]) == 64
                for entry in lock[LOCK_FIELDS["files"]]
            )
        )
        self.assertTrue(result["pass"])

    def test_release_directory_is_create_once(self) -> None:
        first = self.build()
        second = self.build()

        self.assertTrue(first["pass"])
        self.assertFalse(second["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseAlreadyExists"], second["errorCode"]
        )

    def test_one_dot_zero_release_requires_verified_readiness_before_publication(
        self,
    ) -> None:
        prepare_stable_candidate_source(self.source)
        self.git_commit = commit_source(self.source)

        result = self.build()

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseReadinessRequired"], result["errorCode"]
        )
        self.assertFalse(
            (self.dist / "produce-meme-template" / STABLE_VERSION).exists()
        )

    def test_one_dot_zero_candidate_can_be_staged_without_publication(self) -> None:
        prepare_stable_candidate_source(self.source)
        self.git_commit = commit_source(self.source)
        candidates = self.root / "candidates"

        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            result = stage_release(
                self.source,
                candidates,
                git_commit=self.git_commit,
                comparison_base_git_commit=review_base(self.source),
                built_at=BUILT_AT,
            )

        self.assertTrue(result["pass"])
        self.assertTrue(result["candidate"])
        self.assertTrue(Path(result["packageDir"]).is_dir())
        self.assertFalse(
            (self.dist / "produce-meme-template" / STABLE_VERSION).exists()
        )

    def test_stable_candidate_rejects_empty_or_non_ancestor_review_base(self) -> None:
        prepare_stable_candidate_source(self.source)
        self.git_commit = commit_source(self.source)

        for invalid_base in (self.git_commit, "f" * 40):
            with self.subTest(invalid_base=invalid_base):
                result = stage_release(
                    self.source,
                    self.root / ("candidates-" + invalid_base[:8]),
                    git_commit=self.git_commit,
                    comparison_base_git_commit=invalid_base,
                    built_at=BUILT_AT,
                )
                self.assertFalse(result["pass"])
                self.assertEqual(
                    RELEASE_ERRORS["invalidReviewComparisonBase"],
                    result["errorCode"],
                )

    def test_candidate_cannot_be_promoted_without_readiness_completion(self) -> None:
        prepare_stable_candidate_source(self.source)
        self.git_commit = commit_source(self.source)
        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            candidate = stage_release(
                self.source,
                self.root / "candidates",
                git_commit=self.git_commit,
                comparison_base_git_commit=review_base(self.source),
                built_at=BUILT_AT,
            )

        result = promote_release(
            candidate["packageDir"],
            self.dist,
            readiness_root=self.root / "missing-readiness",
        )

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseReadinessRequired"], result["errorCode"]
        )
        self.assertTrue(Path(candidate["packageDir"]).is_dir())
        self.assertFalse(
            (self.dist / "produce-meme-template" / STABLE_VERSION).exists()
        )

    def test_compatible_minor_promotes_after_clean_reviews_and_fresh_install(
        self,
    ) -> None:
        prepare_compatible_minor_source(self.source)
        self.git_commit = commit_source(self.source)
        comparison_base = review_base(self.source)
        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            staged = stage_release(
                self.source,
                self.root / "candidates",
                git_commit=self.git_commit,
                comparison_base_git_commit=comparison_base,
                built_at=BUILT_AT,
            )
        candidate = Path(staged["packageDir"])
        workspace = (self.root / "compatible-readiness").resolve()
        workspace.mkdir()
        receipt_fields = RULES["releaseReadinessContract"]["reviewReceiptFields"]
        receipt_contract = RULES["releaseReadinessContract"]
        pin_sha256 = runtime_production_pin_sha256(candidate)
        receipts = {}
        for axis_role in ("standards", "spec"):
            path = workspace / f"{axis_role}-review.json"
            path.write_text(
                json.dumps(
                    {
                        receipt_fields["artifactType"]: receipt_contract[
                            "reviewReceiptArtifactType"
                        ],
                        receipt_fields["schemaVersion"]: RULES["schemaVersion"],
                        receipt_fields["axis"]: receipt_contract["reviewAxes"][
                            axis_role
                        ],
                        receipt_fields["comparisonBaseGitCommit"]: comparison_base,
                        receipt_fields["reviewedGitCommit"]: self.git_commit,
                        receipt_fields["runtimePinSha256"]: pin_sha256,
                        receipt_fields["clean"]: True,
                        receipt_fields["findingCount"]: 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            receipts[axis_role] = path

        qualification = verify_compatible_release_completion(
            workspace,
            candidate_package=candidate,
            standards_review_receipt=receipts["standards"],
            spec_review_receipt=receipts["spec"],
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(qualification),
            stderr="",
        )
        with mock.patch(
            "scripts.produce_meme_template.release_management.subprocess.run",
            return_value=completed,
        ):
            promoted = promote_release(
                candidate,
                self.dist,
                readiness_root=workspace,
                standards_review_receipt=receipts["standards"],
                spec_review_receipt=receipts["spec"],
            )

        self.assertTrue(qualification["pass"], qualification)
        self.assertTrue(promoted["pass"], promoted)
        self.assertEqual(
            READINESS_PROFILES["compatibleMinor"],
            promoted["releaseReadinessProfile"],
        )
        self.assertTrue(Path(promoted["readinessCompletionPath"]).is_file())
        self.assertTrue(
            doctor(qualification["installedRuntimePath"])["pass"]
        )

    def test_major_release_cannot_declare_the_compatible_minor_profile(self) -> None:
        prepare_versioned_source(
            self.source,
            ".".join(str(part) for part in (2, 0, 0)),
        )
        release_path = self.source / "release.json"
        release = load_json(release_path)
        release["releaseReadinessProfile"] = READINESS_PROFILES[
            "compatibleMinor"
        ]
        release_path.write_text(
            json.dumps(release, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.git_commit = commit_source(self.source)

        result = stage_release(
            self.source,
            self.root / "candidates",
            git_commit=self.git_commit,
            comparison_base_git_commit=review_base(self.source),
            built_at=BUILT_AT,
        )

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["invalidReleaseMetadata"], result["errorCode"]
        )
        self.assertFalse((self.root / "candidates").exists())

    def test_verified_candidate_is_promoted_byte_for_byte(self) -> None:
        prepare_stable_candidate_source(self.source)
        self.git_commit = commit_source(self.source)
        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            staged = stage_release(
                self.source,
                self.root / "candidates",
                git_commit=self.git_commit,
                comparison_base_git_commit=review_base(self.source),
                built_at=BUILT_AT,
            )
        candidate = Path(staged["packageDir"])
        readiness = self.root / "readiness"
        readiness.mkdir()
        verifier_result = {
            "pass": True,
            "errorCode": None,
            "reportPath": str(readiness / "release-readiness-report.json"),
            "completionPath": str(readiness / "release-readiness-completion.json"),
            "releaseDigest": staged["releaseDigest"],
            "gitCommit": self.git_commit,
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(verifier_result),
            stderr="",
        )

        with mock.patch(
            "scripts.produce_meme_template.release_management.subprocess.run",
            return_value=completed,
        ) as verifier:
            promoted = promote_release(
                candidate,
                self.dist,
                readiness_root=readiness,
            )

        self.assertTrue(promoted["pass"])
        self.assertEqual(
            RELEASE_CONTRACT["validationTimeoutSeconds"],
            verifier.call_args.kwargs["timeout"],
        )
        package = Path(promoted["packageDir"])
        lock = load_json(candidate / LOCK_NAME)
        for entry in lock[LOCK_FIELDS["files"]]:
            relative = entry[FILE_FIELDS["path"]]
            self.assertEqual(
                (candidate / relative).read_bytes(),
                (package / relative).read_bytes(),
            )
        self.assertEqual(
            (candidate / LOCK_NAME).read_bytes(),
            (package / LOCK_NAME).read_bytes(),
        )
        command = verifier.call_args.args[0]
        self.assertEqual(candidate / "scripts" / "release_tool.py", Path(command[2]))
        self.assertEqual(candidate, verifier.call_args.kwargs["cwd"])

    def test_promotion_rejects_nonzero_candidate_verifier_exit(self) -> None:
        prepare_stable_candidate_source(self.source)
        self.git_commit = commit_source(self.source)
        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            staged = stage_release(
                self.source,
                self.root / "candidates",
                git_commit=self.git_commit,
                comparison_base_git_commit=review_base(self.source),
                built_at=BUILT_AT,
            )
        verifier_result = {
            "pass": True,
            "reportPath": str(self.root / "report.json"),
            "completionPath": str(self.root / "completion.json"),
            "releaseDigest": staged["releaseDigest"],
            "gitCommit": self.git_commit,
        }
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=2,
            stdout=json.dumps(verifier_result),
            stderr="candidate verifier failed",
        )

        with mock.patch(
            "scripts.produce_meme_template.release_management.subprocess.run",
            return_value=completed,
        ):
            result = promote_release(
                staged["packageDir"],
                self.dist,
                readiness_root=self.root / "readiness",
            )

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseReadinessRequired"], result["errorCode"]
        )

    def test_release_is_frozen_before_atomic_publication(self) -> None:
        with mock.patch(
            "scripts.produce_meme_template.release_management._make_runtime_read_only",
            side_effect=OSError("chmod interrupted"),
        ), mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            interrupted = build_release(
                self.source,
                self.dist,
                git_commit=self.git_commit,
                built_at=BUILT_AT,
            )

        package = (
            self.dist
            / load_json(self.source / "release.json")["skillName"]
            / load_json(self.source / "release.json")["skillVersion"]
        )
        self.assertFalse(interrupted["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseValidationFailure"],
            interrupted["errorCode"],
        )
        self.assertFalse(package.exists())
        self.assertTrue(self.build()["pass"])

        package.chmod(package.stat().st_mode | 0o200)
        diagnosis = doctor(package)
        self.assertFalse(diagnosis["pass"])
        self.assertIn(
            RELEASE_ERRORS["mutableReleaseRuntime"],
            diagnosis[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_release_binds_real_head_and_complete_git_file_set(self) -> None:
        wrong_head = build_release(
            self.source,
            self.dist,
            git_commit="0" * 40,
            built_at=BUILT_AT,
        )

        self.assertFalse(wrong_head["pass"])
        self.assertEqual(
            RELEASE_ERRORS["sourceGitMismatch"], wrong_head["errorCode"]
        )

        manifest_path = self.source / "skill-manifest.json"
        manifest = load_json(manifest_path)
        manifest["tracked_files"].remove("AGENTS.md")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.git_commit = commit_source(self.source)

        incomplete = self.build()

        self.assertFalse(incomplete["pass"])
        self.assertEqual(
            RELEASE_ERRORS["sourceFileSetMismatch"],
            incomplete["errorCode"],
        )
        incomplete_diagnosis = doctor(self.source)
        self.assertFalse(incomplete_diagnosis["pass"])
        self.assertIn(
            RELEASE_ERRORS["invalidSourceRuntime"],
            incomplete_diagnosis[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_source_doctor_rejects_an_unrelated_parent_git_repository(self) -> None:
        parent = self.root / "unrelated-parent"
        nested = copy_release_source(parent / "nested-runtime")
        commit_source(parent)

        diagnosis = doctor(nested)

        self.assertFalse(diagnosis["pass"])
        self.assertIn(
            RELEASE_ERRORS["invalidSourceRuntime"],
            diagnosis[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_release_runs_the_complete_test_suite(self) -> None:
        if os.environ.get(VALIDATION_SUITE_ENV) == "1":
            self.skipTest("release validation cannot recursively validate itself")
        broken_test = self.source / "tests" / "test_issue_12_batch_isolation.py"
        broken_test.write_text(
            "raise RuntimeError('release validation sentinel')\n",
            encoding="utf-8",
        )
        self.git_commit = commit_source(self.source)

        result = build_release(
            self.source,
            self.dist,
            git_commit=self.git_commit,
            built_at=BUILT_AT,
        )

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseValidationFailure"], result["errorCode"]
        )

    def test_release_rejects_unsafe_or_symlinked_tracked_files(self) -> None:
        manifest_path = self.source / "skill-manifest.json"
        manifest = load_json(manifest_path)
        manifest["tracked_files"].append("../outside.txt")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        unsafe = self.build()

        self.assertFalse(unsafe["pass"])
        self.assertEqual(
            RELEASE_ERRORS["invalidReleaseMetadata"], unsafe["errorCode"]
        )

        self.source = copy_release_source(self.root / "symlink-source")
        prepare_development_source(self.source)
        self.git_commit = commit_source(self.source)
        external = self.root / "external-skill.md"
        external.write_text("external", encoding="utf-8")
        skill_path = self.source / "SKILL.md"
        skill_path.unlink()
        skill_path.symlink_to(external)
        self.git_commit = commit_source(self.source)

        symlinked = self.build()

        self.assertFalse(symlinked["pass"])
        self.assertEqual(
            RELEASE_ERRORS["missingReleaseFile"], symlinked["errorCode"]
        )

    def test_install_resumes_after_version_directory_was_renamed(self) -> None:
        package = Path(self.build()["packageDir"])
        release = load_json(package / "release.json")
        target = (
            self.install_root
            / RELEASE_CONTRACT["installLayout"]["versionsDirectory"]
            / release["skillVersion"]
        )
        shutil.copytree(package, target)

        resumed = install_verified(package, self.install_root)

        self.assertTrue(resumed["pass"])
        self.assertEqual(target.resolve(), Path(resumed["currentDir"]).resolve())

    def test_install_rejects_package_overlap_and_untracked_bytecode(self) -> None:
        package = Path(self.build()["packageDir"])
        lock = load_json(package / LOCK_NAME)
        overlap = install_release(
            package,
            package / "install",
            expected_release_digest=lock[LOCK_FIELDS["releaseDigest"]],
        )

        self.assertFalse(overlap["pass"])
        self.assertEqual(
            RELEASE_ERRORS["installPathConflict"], overlap["errorCode"]
        )

        current = Path(install_verified(package, self.install_root)["currentDir"])
        current.chmod(current.stat().st_mode | 0o200)
        current.joinpath("evil.pyc").write_bytes(b"untracked")
        diagnosis = doctor(current)
        self.assertFalse(diagnosis["pass"])
        self.assertIn(
            RELEASE_ERRORS["extraInstallFile"],
            diagnosis[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_install_smoke_blocks_a_self_consistent_broken_package(self) -> None:
        package = Path(self.build()["packageDir"])
        entrypoint = package / "scripts" / "produce.py"
        entrypoint.chmod(entrypoint.stat().st_mode | 0o200)
        entrypoint.write_text("this is invalid python ?\n", encoding="utf-8")
        expected_digest = rebind_release_lock(package, "scripts/produce.py")
        for path in sorted(package.rglob("*"), reverse=True):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)
        package.chmod(package.stat().st_mode & ~0o222)

        result = install_release(
            package,
            self.install_root,
            expected_release_digest=expected_digest,
        )

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseValidationFailure"], result["errorCode"]
        )
        self.assertFalse(
            (self.install_root / RELEASE_CONTRACT["installLayout"]["currentPointer"]).exists()
        )

    def test_install_freeze_failure_is_stable_and_retryable(self) -> None:
        package = Path(self.build()["packageDir"])
        lock = load_json(package / LOCK_NAME)
        release_digest = lock[LOCK_FIELDS["releaseDigest"]]
        with mock.patch(
            "scripts.produce_meme_template.release_management._make_runtime_read_only",
            side_effect=OSError("chmod interrupted"),
        ):
            interrupted = install_release(
                package,
                self.install_root,
                expected_release_digest=release_digest,
            )

        versions = self.install_root / RELEASE_CONTRACT["installLayout"][
            "versionsDirectory"
        ]
        self.assertFalse(interrupted["pass"])
        self.assertEqual(
            RELEASE_ERRORS["releaseValidationFailure"],
            interrupted["errorCode"],
        )
        self.assertEqual([], list(versions.glob(".*")))
        self.assertTrue(
            install_release(
                package,
                self.install_root,
                expected_release_digest=release_digest,
            )["pass"]
        )

    def test_malformed_release_and_pin_shapes_return_stable_failures(self) -> None:
        package = Path(self.build()["packageDir"])
        rules_path = package / "contracts" / "machine-rules.json"
        rules_path.chmod(rules_path.stat().st_mode | 0o200)
        package_rules = load_json(rules_path)
        del package_rules["releaseManagementContract"]["lockFileName"]
        rules_path.write_text(
            json.dumps(package_rules, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        installed = install_verified(package, self.install_root)
        diagnosis = doctor(package)

        self.assertFalse(installed["pass"])
        self.assertEqual(
            RELEASE_ERRORS["invalidReleaseLock"], installed["errorCode"]
        )
        self.assertFalse(diagnosis["pass"])
        self.assertIn(
            RELEASE_ERRORS["invalidReleaseLock"],
            diagnosis[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

        fresh_source = copy_release_source(self.root / "fresh-source")
        commit_source(fresh_source)
        self.assertIn(
            RELEASE_ERRORS["invalidProductionPin"],
            doctor(fresh_source, production_pin=[])[
                DIAGNOSTIC_FIELDS["errorCodes"]
            ],
        )
        item_dir = self.root / "malformed-pin-item"
        item_dir.mkdir()
        (item_dir / "production-pin.json").write_text(
            json.dumps({"skill": []}), encoding="utf-8"
        )
        migration = write_pin_migration_report(item_dir, fresh_source)
        self.assertFalse(migration["pass"])
        self.assertEqual(
            RELEASE_ERRORS["invalidProductionPin"], migration["errorCode"]
        )

    def test_release_cli_returns_json_for_invalid_pin_and_timestamp(self) -> None:
        invalid_pin = self.root / "invalid-pin.json"
        invalid_pin.write_text("{bad", encoding="utf-8")
        doctor_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "release_tool.py"),
                "doctor",
                "--runtime",
                str(self.source),
                "--production-pin",
                str(invalid_pin),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        build_result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "release_tool.py"),
                "build",
                "--source",
                str(self.source),
                "--dist",
                str(self.dist),
                "--built-at",
                "invalid",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(1, doctor_result.returncode)
        self.assertEqual(
            RELEASE_ERRORS["invalidProductionPin"],
            json.loads(doctor_result.stdout)["errorCode"],
        )
        self.assertEqual(1, build_result.returncode)
        self.assertEqual(
            RELEASE_ERRORS["invalidReleaseMetadata"],
            json.loads(build_result.stdout)["errorCode"],
        )

    def test_release_cli_exposes_stage_and_promote_commands(self) -> None:
        for command in ("stage", "verify-readiness", "promote"):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "release_tool.py"),
                    command,
                    "--help",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)

    def test_install_and_doctor_detect_missing_extra_and_drifted_files(self) -> None:
        package = Path(self.build()["packageDir"])
        installed = install_verified(package, self.install_root)
        current = Path(installed["currentDir"])

        self.assertTrue(installed["pass"])
        self.assertTrue(doctor(current)["pass"])

        drifted = current / "SKILL.md"
        drifted.chmod(drifted.stat().st_mode | 0o200)
        drifted.write_text(drifted.read_text(encoding="utf-8") + "\nlocal patch\n", encoding="utf-8")
        self.assertIn(
            RELEASE_ERRORS["fileDigestMismatch"],
            doctor(current)[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

        drifted.write_bytes(package.joinpath("SKILL.md").read_bytes())
        current.chmod(current.stat().st_mode | 0o200)
        current.joinpath("release.json").unlink()
        self.assertIn(
            RELEASE_ERRORS["missingReleaseFile"],
            doctor(current)[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

        current.joinpath("release.json").write_bytes(
            package.joinpath("release.json").read_bytes()
        )
        current.joinpath("untracked-local.txt").write_text("drift", encoding="utf-8")
        self.assertIn(
            RELEASE_ERRORS["extraInstallFile"],
            doctor(current)[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_doctor_rejects_mixed_version_and_pin_mismatch(self) -> None:
        package = Path(self.build()["packageDir"])
        current = Path(install_verified(package, self.install_root)["currentDir"])
        pin = runtime_production_pin(current)

        self.assertTrue(doctor(current, production_pin=pin)["pass"])
        mismatched = copy.deepcopy(pin)
        mismatched[
            RELEASE_CONTRACT["productionPinFields"][
                "artifactSchemaVersion"
            ]
        ] = "99.0.0"
        report = doctor(current, production_pin=mismatched)
        self.assertFalse(report["pass"])
        self.assertIn(
            RELEASE_ERRORS["productionPinMismatch"],
            report[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_doctor_rejects_self_consistent_lock_with_mixed_release_metadata(
        self,
    ) -> None:
        package = Path(self.build()["packageDir"])
        release_path = package / "release.json"
        release_path.chmod(release_path.stat().st_mode | 0o200)
        release = load_json(release_path)
        release["skillVersion"] = "9.9.9"
        release_bytes = (
            json.dumps(release, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        release_path.write_bytes(release_bytes)
        rebind_release_lock(package, "release.json")

        report = doctor(package)

        self.assertFalse(report["pass"])
        self.assertIn(
            RELEASE_ERRORS["mixedVersion"],
            report[DIAGNOSTIC_FIELDS["errorCodes"]],
        )

    def test_migration_report_preserves_old_pin_and_records_version_diff(self) -> None:
        package = Path(self.build()["packageDir"])
        current = Path(install_verified(package, self.install_root)["currentDir"])
        item_dir = self.root / "production-item"
        item_dir.mkdir()
        old_pin = runtime_production_pin(current)
        pin_fields = RELEASE_CONTRACT["productionPinFields"]
        skill_fields = RELEASE_CONTRACT["productionPinSkillFields"]
        old_pin[pin_fields["skill"]][skill_fields["version"]] = "0.11.0"
        old_pin[pin_fields["artifactSchemaVersion"]] = "0.11.0"
        old_pin[pin_fields["releaseSha256"]] = "a" * 64
        for role in (
            "gitCommit",
            "validatorSha256",
            "replacementSpecVersion",
            "replacementSpecSha256",
        ):
            old_pin.pop(pin_fields[role])
        pin_path = item_dir / "production-pin.json"
        pin_path.write_text(json.dumps(old_pin), encoding="utf-8")
        (item_dir / "gallery-template.json").write_text(
            json.dumps({"key": "migration-template"}), encoding="utf-8"
        )
        (item_dir / "final-validation-report.json").write_text(
            json.dumps({"pass": True}), encoding="utf-8"
        )
        manifest_path = item_dir / "production-manifest.json"
        item_id = "migration-item"
        artifacts = {
            "production-pin.json": production_artifact_record(
                item_dir,
                item_id,
                "production-pin.json",
                revision=1,
                phase=RULES["productionPhases"][0]["phase"],
            ),
            "gallery-template.json": production_artifact_record(
                item_dir,
                item_id,
                "gallery-template.json",
                revision=3,
                phase=RULES["productionPhases"][-1]["phase"],
            ),
            "final-validation-report.json": production_artifact_record(
                item_dir,
                item_id,
                "final-validation-report.json",
                revision=3,
                phase=RULES["productionPhases"][-1]["phase"],
            ),
        }
        manifest_path.write_text(
            json.dumps(
                {
                    "artifactType": "production-manifest",
                    "productionItemId": item_id,
                    "templateKey": "migration-template",
                    "revision": 3,
                    "sourceImageSha256": "1" * 64,
                    "replacementStrategySha256": "2" * 64,
                    "generationOptionsSha256": "3" * 64,
                    "phase": RULES["productionPhases"][-1]["phase"],
                    "state": RULES["resultStates"]["completed"],
                    "outcome": "completed",
                    "history": [
                        {
                            "phase": RULES["productionPhases"][-1]["phase"],
                            "state": RULES["productionPhases"][-1]["state"],
                            "at": BUILT_AT.isoformat(),
                        }
                    ],
                    "artifacts": artifacts,
                }
            ),
            encoding="utf-8",
        )

        result = write_pin_migration_report(item_dir, current)

        self.assertTrue(result["pass"])
        self.assertEqual(old_pin, load_json(pin_path))
        report = load_json(Path(result["reportPath"]))
        self.assertEqual(
            hashlib.sha256(pin_path.read_bytes()).hexdigest(),
            report[MIGRATION_FIELDS["oldPinSha256"]],
        )
        self.assertEqual(
            "migration-item", report[MIGRATION_FIELDS["productionItemId"]]
        )
        self.assertEqual(3, report[MIGRATION_FIELDS["templateRevision"]])
        self.assertEqual(
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            report[MIGRATION_FIELDS["productionManifestSha256"]],
        )
        self.assertEqual(
            RULES["productionPhases"][
                RELEASE_CONTRACT["migrationInvalidateFromPhaseIndex"]
            ]["phase"],
            report[MIGRATION_FIELDS["invalidateFromPhase"]],
        )
        self.assertNotEqual(
            report[MIGRATION_FIELDS["oldPinSha256"]],
            report[MIGRATION_FIELDS["newPinSha256"]],
        )

    def test_migration_rejects_pin_symlink_outside_the_item(self) -> None:
        package = Path(self.build()["packageDir"])
        current = Path(install_verified(package, self.install_root)["currentDir"])
        external_pin = self.root / "external-production-pin.json"
        external_pin.write_text(
            json.dumps(runtime_production_pin(current)), encoding="utf-8"
        )
        item_dir = self.root / "symlink-pin-item"
        item_dir.mkdir()
        (item_dir / "production-pin.json").symlink_to(external_pin)

        result = write_pin_migration_report(item_dir, current)

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["invalidProductionPin"], result["errorCode"]
        )

    def test_migration_accepts_a_valid_in_progress_lineage(self) -> None:
        package = Path(self.build()["packageDir"])
        current = Path(install_verified(package, self.install_root)["currentDir"])
        item_dir = self.root / "in-progress-item"
        (item_dir / "evidence").mkdir(parents=True)
        (item_dir / "production-pin.json").write_text(
            json.dumps(runtime_production_pin(current)), encoding="utf-8"
        )
        (item_dir / "evidence" / "source-image.png").write_bytes(b"source")
        (item_dir / "source-analysis.json").write_text(
            json.dumps({"target": {"role": "subject"}}), encoding="utf-8"
        )
        item_id = "in-progress-item"
        phase = RULES["productionPhases"][0]
        artifacts = {
            relative: production_artifact_record(
                item_dir,
                item_id,
                relative,
                revision=1,
                phase=phase["phase"],
            )
            for relative in (
                "production-pin.json",
                "evidence/source-image.png",
                "source-analysis.json",
            )
        }
        (item_dir / "production-manifest.json").write_text(
            json.dumps(
                {
                    "artifactType": "production-manifest",
                    "productionItemId": item_id,
                    "templateKey": "in-progress-template",
                    "revision": 1,
                    "sourceImageSha256": "1" * 64,
                    "replacementStrategySha256": "2" * 64,
                    "generationOptionsSha256": "3" * 64,
                    "phase": phase["phase"],
                    "state": phase["state"],
                    "outcome": None,
                    "history": [
                        {
                            "phase": phase["phase"],
                            "state": phase["state"],
                            "at": BUILT_AT.isoformat(),
                        }
                    ],
                    "artifacts": artifacts,
                }
            ),
            encoding="utf-8",
        )

        result = write_pin_migration_report(item_dir, current)

        self.assertTrue(result["pass"])
        report = load_json(Path(result["reportPath"]))
        self.assertEqual(1, report[MIGRATION_FIELDS["templateRevision"]])

    def test_migration_rejects_a_manifest_without_pin_lineage(self) -> None:
        package = Path(self.build()["packageDir"])
        current = Path(install_verified(package, self.install_root)["currentDir"])
        item_dir = self.root / "ghost-revision-item"
        item_dir.mkdir()
        (item_dir / "production-pin.json").write_text(
            json.dumps(runtime_production_pin(current)), encoding="utf-8"
        )
        item_id = "ghost"
        pin_record = production_artifact_record(
            item_dir,
            item_id,
            "production-pin.json",
            revision=999,
            phase=RULES["productionPhases"][0]["phase"],
        )
        (item_dir / "production-manifest.json").write_text(
            json.dumps(
                {
                    "artifactType": "production-manifest",
                    "productionItemId": item_id,
                    "templateKey": "ghost-template",
                    "revision": 999,
                    "sourceImageSha256": "1" * 64,
                    "replacementStrategySha256": "2" * 64,
                    "generationOptionsSha256": "3" * 64,
                    "phase": RULES["productionPhases"][-1]["phase"],
                    "state": RULES["resultStates"]["completed"],
                    "outcome": "completed",
                    "history": [
                        {
                            "phase": RULES["productionPhases"][-1]["phase"],
                            "state": RULES["productionPhases"][-1]["state"],
                            "at": BUILT_AT.isoformat(),
                        }
                    ],
                    "artifacts": {"production-pin.json": pin_record},
                }
            ),
            encoding="utf-8",
        )

        result = write_pin_migration_report(item_dir, current)

        self.assertFalse(result["pass"])
        self.assertEqual(
            RELEASE_ERRORS["invalidProductionManifest"],
            result["errorCode"],
        )

    def test_fresh_install_doctor_and_minimal_vertical_slice(self) -> None:
        package = Path(self.build()["packageDir"])
        current = Path(install_verified(package, self.install_root)["currentDir"])
        diagnosis = subprocess.run(
            [
                sys.executable,
                str(current / "scripts" / "release_tool.py"),
                "doctor",
                "--runtime",
                str(current),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, diagnosis.returncode, diagnosis.stderr)
        self.assertTrue(json.loads(diagnosis.stdout)["pass"])

        fixture = current / "fixtures" / "e2e" / "simple-animal"
        production = subprocess.run(
            [
                sys.executable,
                str(current / "scripts" / "produce.py"),
                "--request",
                str(fixture / "request.json"),
                "--deterministic-fixture",
                str(fixture),
                "--output",
                str(self.root / "installed-output"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, production.returncode, production.stderr)
        payload = json.loads(production.stdout)
        self.assertEqual("completed", payload["outcome"])
        self.assertTrue(
            (self.root / "installed-output" / payload["productionItemId"] / "gallery-template.json").is_file()
        )

    def test_production_resume_blocks_when_runtime_no_longer_matches_pin(self) -> None:
        request = load_json(FIXTURE / "request.json")
        request["sourceImage"] = str(FIXTURE / request["sourceImage"])
        adapters = DeterministicFixtureAdapters(FIXTURE)
        output = self.root / "runtime-pin-output"
        first = run_production(request, output, adapters, clock=lambda: BUILT_AT)
        pin_path = first.output_dir / "production-pin.json"
        pin = load_json(pin_path)
        pin_fields = RELEASE_CONTRACT["productionPinFields"]
        skill_fields = RELEASE_CONTRACT["productionPinSkillFields"]
        pin[pin_fields["skill"]][skill_fields["version"]] = "99.0.0"
        pin_path.write_text(
            json.dumps(pin, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        upload_count = len(adapters.upload_calls)

        resumed = run_production(request, output, adapters, clock=lambda: BUILT_AT)

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(
            RULES["errorCodes"]["versionDiagnosticFailure"],
            resumed.error_code,
        )
        self.assertEqual(upload_count, len(adapters.upload_calls))


if __name__ == "__main__":
    unittest.main()
