from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    compact_json_line_bytes as _json_bytes,
    load_json_object as _load_object,
    pretty_json_bytes as _pretty_json_bytes,
    sha256_bytes as _sha_bytes,
    sha256_file as _sha_file,
)

MACHINE_RULES_RELATIVE = Path("contracts") / "machine-rules.json"
RELEASE_CONTRACT_SCHEMA_RELATIVE = (
    Path("contracts") / "release-management-contract.schema.json"
)
SEMVER_PATTERN = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
VALIDATION_SUITE_ENV = "PRODUCE_MEME_TEMPLATE_VALIDATION_SUITE"


def _runtime_contract() -> dict[str, Any]:
    runtime_root = Path(__file__).resolve().parents[2]
    return _release_contract_view(
        _load_object(runtime_root / MACHINE_RULES_RELATIVE).get(
            "releaseManagementContract"
        ),
        contract_root=runtime_root,
    )


def runtime_release_contract() -> dict[str, Any]:
    return copy.deepcopy(_runtime_contract())


def _safe_relative_path(value: str) -> bool:
    path = Path(value)
    return bool(
        value
        and "\\" not in value
        and not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _release_contract_view(
    value: Any,
    *,
    contract_root: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("release management contract must be an object")
    root = contract_root or Path(__file__).resolve().parents[2]
    schema = _load_object(root / RELEASE_CONTRACT_SCHEMA_RELATIVE)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].absolute_path)
        raise ValueError(
            f"invalid release management contract at {location or '<root>'}: "
            f"{errors[0].message}"
        )
    for mapping_name, mapping in value.items():
        if isinstance(mapping, dict) and len(mapping) != len(set(mapping.values())):
            raise ValueError(
                f"release contract mapping values must be unique: {mapping_name}"
            )
    if not set(value["legacyProductionPinRequiredFieldRoles"]) <= set(
        value["productionPinFields"]
    ):
        raise ValueError(
            "legacy production pin roles must exist in productionPinFields"
        )
    return value


def _production_pin_shape_valid(
    value: Any,
    contract: dict[str, Any],
    *,
    allow_legacy: bool = False,
) -> bool:
    fields = contract["productionPinFields"]
    skill_fields = contract["productionPinSkillFields"]
    gallery_fields = contract["productionPinGalleryFields"]
    current_keys = set(fields.values())
    legacy_keys = {
        fields[role]
        for role in contract["legacyProductionPinRequiredFieldRoles"]
    }
    if not isinstance(value, dict) or (
        set(value) != current_keys
        and not (allow_legacy and set(value) == legacy_keys)
    ):
        return False
    skill = value.get(fields["skill"])
    gallery = value.get(fields["galleryContract"])
    sha_roles = (
        "releaseSha256",
        "releaseManifestSha256",
        "machineRulesSha256",
        "validatorSha256",
        "replacementSpecSha256",
    )
    if not (
        isinstance(skill, dict)
        and set(skill) == set(skill_fields.values())
        and skill.get(skill_fields["name"]) == "produce-meme-template"
        and isinstance(skill.get(skill_fields["version"]), str)
        and SEMVER_PATTERN.fullmatch(skill[skill_fields["version"]])
        and isinstance(gallery, dict)
        and set(gallery) == set(gallery_fields.values())
        and isinstance(gallery.get(gallery_fields["id"]), str)
        and gallery[gallery_fields["id"]].strip()
        and isinstance(gallery.get(gallery_fields["snapshot"]), str)
        and _safe_relative_path(gallery[gallery_fields["snapshot"]])
        and all(
            isinstance(gallery.get(gallery_fields[role]), str)
            and re.fullmatch(
                r"[0-9a-f]{64}", gallery[gallery_fields[role]]
            )
            for role in ("sha256", "upstreamSourceSha256")
        )
        and value.get(fields["artifactType"])
        == contract["productionPinArtifactType"]
        and isinstance(value.get(fields["schemaVersion"]), str)
        and SEMVER_PATTERN.fullmatch(value[fields["schemaVersion"]])
        and isinstance(value.get(fields["artifactSchemaVersion"]), str)
        and SEMVER_PATTERN.fullmatch(
            value[fields["artifactSchemaVersion"]]
        )
        and all(
            isinstance(value.get(fields[role]), str)
            and re.fullmatch(r"[0-9a-f]{64}", value[fields[role]])
            for role in sha_roles
            if fields[role] in value
        )
        and isinstance(value.get(fields["releaseFileCount"]), int)
        and not isinstance(value[fields["releaseFileCount"]], bool)
        and value[fields["releaseFileCount"]] > 0
    ):
        return False
    if set(value) == current_keys and not (
        isinstance(value.get(fields["replacementSpecVersion"]), str)
        and SEMVER_PATTERN.fullmatch(
            value[fields["replacementSpecVersion"]]
        )
        and isinstance(value.get(fields["gitCommit"]), str)
        and re.fullmatch(r"[0-9a-f]{40}", value[fields["gitCommit"]])
    ):
        return False
    return True


def _relative_files(root: Path, contract: dict[str, Any]) -> set[str]:
    ignored_directories = set(contract["runtimeIgnoredDirectoryNames"])
    ignored_suffixes = set(contract["runtimeIgnoredSuffixes"])
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
        if ignored_directories.isdisjoint(path.relative_to(root).parts)
        and path.suffix not in ignored_suffixes
    }


def _make_runtime_read_only(runtime: Path) -> None:
    for path in sorted(runtime.rglob("*"), reverse=True):
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    runtime.chmod(runtime.stat().st_mode & ~0o222)


def _runtime_is_read_only(runtime: Path) -> bool:
    return all(
        path.is_symlink() or path.stat().st_mode & 0o222 == 0
        for path in (runtime, *runtime.rglob("*"))
    )


def _discard_staging_tree(staging: Path) -> None:
    if not staging.exists():
        return
    try:
        staging.chmod(staging.stat().st_mode | 0o700)
        for root, directories, files in os.walk(staging):
            root_path = Path(root)
            root_path.chmod(root_path.stat().st_mode | 0o700)
            for name in directories:
                path = root_path / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
            for name in files:
                path = root_path / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o600)
    except OSError:
        pass
    shutil.rmtree(staging, ignore_errors=True)


def _release_metadata(source_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    release = _load_object(source_root / "release.json")
    manifest = _load_object(source_root / "skill-manifest.json")
    rules = _load_object(source_root / MACHINE_RULES_RELATIVE)
    skill_version = release.get("skillVersion")
    artifact_version = release.get("artifactSchemaVersion")
    supported_contracts = release.get("supportedContracts")
    gallery_version = (
        supported_contracts.get("galleryTemplate")
        if isinstance(supported_contracts, dict)
        else None
    )
    _release_contract_view(
        rules.get("releaseManagementContract"), contract_root=source_root
    )
    if not (
        release.get("skillName") == "produce-meme-template"
        and isinstance(skill_version, str)
        and SEMVER_PATTERN.fullmatch(skill_version)
        and isinstance(artifact_version, str)
        and SEMVER_PATTERN.fullmatch(artifact_version)
        and isinstance(gallery_version, str)
        and gallery_version.strip()
        and rules.get("schemaVersion") == artifact_version
        and manifest.get("version") == skill_version
    ):
        raise ValueError("release, artifact schema, gallery contract, and manifest versions disagree")
    tracked = manifest.get("tracked_files")
    if not (
        isinstance(tracked, list)
        and tracked
        and all(isinstance(value, str) and value for value in tracked)
        and all(_safe_relative_path(value) for value in tracked)
        and len(tracked) == len(set(tracked))
    ):
        raise ValueError("skill manifest tracked_files must be a unique string list")
    return release, manifest, rules


def _git_snapshot(source_root: Path) -> dict[str, Any] | None:
    commands = {
        "root": ["git", "rev-parse", "--show-toplevel"],
        "head": ["git", "rev-parse", "HEAD"],
        "files": [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        "status": [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=all",
        ],
    }
    completed = {
        role: subprocess.run(
            command,
            cwd=source_root,
            capture_output=True,
            check=False,
        )
        for role, command in commands.items()
    }
    if any(result.returncode != 0 for result in completed.values()):
        return None
    try:
        git_root = Path(
            completed["root"].stdout.decode("utf-8").strip()
        ).resolve()
        head = completed["head"].stdout.decode("ascii").strip()
        files = {
            value.decode("utf-8")
            for value in completed["files"].stdout.split(b"\0")
            if value
        }
    except (UnicodeDecodeError, OSError):
        return None
    if git_root != source_root:
        return None
    return {
        "head": head,
        "files": files,
        "clean": not completed["status"].stdout.strip(),
    }


def _git_blobs(
    source_root: Path, git_commit: str, tracked: list[str]
) -> dict[str, bytes] | None:
    blobs: dict[str, bytes] = {}
    for relative in tracked:
        completed = subprocess.run(
            ["git", "show", f"{git_commit}:{relative}"],
            cwd=source_root,
            capture_output=True,
            check=False,
        )
        path = source_root / relative
        if (
            completed.returncode != 0
            or not path.is_file()
            or path.is_symlink()
            or path.read_bytes() != completed.stdout
        ):
            return None
        blobs[relative] = completed.stdout
    return blobs


def _run_release_validation(
    runtime_root: Path,
    contract: dict[str, Any],
    *,
    include_tests: bool,
) -> tuple[bool, str | None]:
    timeout = contract["validationTimeoutSeconds"]
    fixture = runtime_root / contract["smokeFixtureRelativePath"]
    commands: list[list[str]] = []
    if include_tests:
        commands.append(
            [
                sys.executable,
                contract["buildValidationRunnerRelativePath"],
            ]
        )
    with tempfile.TemporaryDirectory() as temporary:
        commands.append(
            [
                sys.executable,
                "scripts/produce.py",
                "--request",
                str(fixture / "request.json"),
                "--deterministic-fixture",
                str(fixture),
                "--output",
                str(Path(temporary) / "output"),
            ]
        )
        for command in commands:
            try:
                completed = subprocess.run(
                    command,
                    cwd=runtime_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout,
                    env={
                        **os.environ,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        VALIDATION_SUITE_ENV: "1",
                    },
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, type(exc).__name__
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                return False, detail[-2000:]
    return True, None


def _lock_without_digest(
    lock: dict[str, Any], lock_fields: dict[str, str]
) -> dict[str, Any]:
    digest_field = lock_fields["releaseDigest"]
    return {key: value for key, value in lock.items() if key != digest_field}


def _lock_digest(lock: dict[str, Any], lock_fields: dict[str, str]) -> str:
    return _sha_bytes(_json_bytes(_lock_without_digest(lock, lock_fields)))


def _content_digest(
    files: list[dict[str, Any]],
    lock_fields: dict[str, str],
    file_fields: dict[str, str],
) -> str:
    return _sha_bytes(
        _canonical_bytes(
            {
                lock_fields["files"]: [
                    {
                        file_fields["path"]: entry[file_fields["path"]],
                        file_fields["sha256"]: entry[file_fields["sha256"]],
                    }
                    for entry in files
                ]
            }
        )
    )


def _build_release_package(
    source_root: str | Path,
    dist_root: str | Path,
    *,
    git_commit: str,
    comparison_base_git_commit: str | None,
    built_at: datetime | None = None,
    candidate: bool,
) -> dict[str, Any]:
    source = Path(source_root).resolve()
    dist = Path(dist_root).resolve()
    try:
        release, manifest, rules = _release_metadata(source)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"][
                "invalidReleaseMetadata"
            ],
            "message": str(exc),
        }
    release_contract = _release_contract_view(
        rules.get("releaseManagementContract"), contract_root=source
    )
    codes = release_contract["errorCodes"]
    if int(release["skillVersion"].split(".", 1)[0]) >= 1 and not candidate:
        return {
            "pass": False,
            "errorCode": codes["releaseReadinessRequired"],
            "message": (
                "stable releases require a verified release-readiness "
                "completion and promotion"
            ),
        }
    lock_fields = release_contract["lockFields"]
    file_fields = release_contract["fileFields"]
    gallery_relative = release_contract["gallerySnapshotRelativePath"]
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        return {"pass": False, "errorCode": codes["invalidGitCommit"], "message": "git commit must be 40 lowercase hex characters"}
    git_snapshot = _git_snapshot(source)
    if git_snapshot is None or git_snapshot["head"] != git_commit:
        return {
            "pass": False,
            "errorCode": codes["sourceGitMismatch"],
            "message": "source must be the root of the requested Git HEAD",
        }
    if not git_snapshot["clean"]:
        return {"pass": False, "errorCode": codes["dirtyWorktree"], "message": "release requires a clean Git worktree"}
    stable_candidate = bool(
        candidate and int(release["skillVersion"].split(".", 1)[0]) >= 1
    )
    if stable_candidate:
        valid_base_shape = bool(
            isinstance(comparison_base_git_commit, str)
            and re.fullmatch(r"[0-9a-f]{40}", comparison_base_git_commit)
            and comparison_base_git_commit != git_commit
        )
        ancestry = (
            subprocess.run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    comparison_base_git_commit or "",
                    git_commit,
                ],
                cwd=source,
                capture_output=True,
                check=False,
            )
            if valid_base_shape
            else None
        )
        if ancestry is None or ancestry.returncode != 0:
            return {
                "pass": False,
                "errorCode": codes["invalidReviewComparisonBase"],
                "message": (
                    "stable candidate review base must be a distinct ancestor "
                    "of the reviewed Git commit"
                ),
            }
    if git_snapshot["files"] != set(manifest["tracked_files"]):
        return {
            "pass": False,
            "errorCode": codes["sourceFileSetMismatch"],
            "message": "skill manifest must exactly match Git tracked files",
        }
    tracked = manifest["tracked_files"]
    missing = [
        relative
        for relative in tracked
        if not (source / relative).is_file()
        or (source / relative).is_symlink()
        or not (source / relative).resolve().is_relative_to(source)
    ]
    if missing:
        return {
            "pass": False,
            "errorCode": codes["missingReleaseFile"],
            "missingFiles": missing,
        }
    package = dist / release["skillName"] / release["skillVersion"]
    if package.exists():
        return {
            "pass": False,
            "errorCode": codes["releaseAlreadyExists"],
            "message": "published version directories are immutable",
            "packageDir": str(package),
        }
    validation_passed, validation_detail = _run_release_validation(
        source, release_contract, include_tests=True
    )
    if not validation_passed:
        return {
            "pass": False,
            "errorCode": codes["releaseValidationFailure"],
            "message": validation_detail,
        }
    verified_snapshot = _git_snapshot(source)
    git_blobs = _git_blobs(source, git_commit, tracked)
    if not (
        verified_snapshot is not None
        and verified_snapshot == git_snapshot
        and verified_snapshot["clean"]
        and git_blobs is not None
    ):
        return {
            "pass": False,
            "errorCode": codes["sourceGitMismatch"],
            "message": "source changed while release validation was running",
        }
    files = [
        {
            file_fields["path"]: relative,
            file_fields["sha256"]: _sha_bytes(git_blobs[relative]),
            file_fields["bytes"]: len(git_blobs[relative]),
        }
        for relative in tracked
    ]
    lock = {
        lock_fields["artifactType"]: release_contract["lockArtifactType"],
        lock_fields["lockSchemaVersion"]: release_contract["lockSchemaVersion"],
        lock_fields["skillName"]: release["skillName"],
        lock_fields["skillVersion"]: release["skillVersion"],
        lock_fields["artifactSchemaVersion"]: release["artifactSchemaVersion"],
        lock_fields["galleryContractVersion"]: release["supportedContracts"]["galleryTemplate"],
        lock_fields["galleryContractSha256"]: _sha_bytes(
            git_blobs[gallery_relative]
        ),
        lock_fields["gitCommit"]: git_commit,
        lock_fields["reviewComparisonBaseGitCommit"]: (
            comparison_base_git_commit if stable_candidate else None
        ),
        lock_fields["builtAt"]: (built_at or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        lock_fields["files"]: files,
        lock_fields["contentDigest"]: _content_digest(
            files, lock_fields, file_fields
        ),
    }
    lock[lock_fields["releaseDigest"]] = _lock_digest(lock, lock_fields)
    package.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{release['skillVersion']}.",
            dir=package.parent,
        )
    )
    try:
        for relative in tracked:
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(git_blobs[relative])
        (staging / release_contract["lockFileName"]).write_bytes(
            _json_bytes(lock)
        )
        _make_runtime_read_only(staging)
        try:
            staging.rename(package)
        except OSError:
            if package.exists():
                _discard_staging_tree(staging)
                return {
                    "pass": False,
                    "errorCode": codes["releaseAlreadyExists"],
                    "packageDir": str(package),
                }
            raise
    except OSError as exc:
        _discard_staging_tree(staging)
        return {
            "pass": False,
            "errorCode": codes["releaseValidationFailure"],
            "message": f"release staging could not be frozen: {type(exc).__name__}",
        }
    return {
        "pass": True,
        "candidate": candidate,
        "packageDir": str(package),
        "releaseDigest": lock[lock_fields["releaseDigest"]],
        "contentDigest": lock[lock_fields["contentDigest"]],
        "skillVersion": lock[lock_fields["skillVersion"]],
        "artifactSchemaVersion": lock[lock_fields["artifactSchemaVersion"]],
        "galleryContractVersion": lock[lock_fields["galleryContractVersion"]],
    }


def build_release(
    source_root: str | Path,
    dist_root: str | Path,
    *,
    git_commit: str,
    built_at: datetime | None = None,
) -> dict[str, Any]:
    return _build_release_package(
        source_root,
        dist_root,
        git_commit=git_commit,
        comparison_base_git_commit=None,
        built_at=built_at,
        candidate=False,
    )


def stage_release(
    source_root: str | Path,
    candidate_root: str | Path,
    *,
    git_commit: str,
    comparison_base_git_commit: str | None = None,
    built_at: datetime | None = None,
) -> dict[str, Any]:
    return _build_release_package(
        source_root,
        candidate_root,
        git_commit=git_commit,
        comparison_base_git_commit=comparison_base_git_commit,
        built_at=built_at,
        candidate=True,
    )


def _verify_release(
    package_dir: Path, *, require_read_only: bool = True
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        rules_path = package_dir / MACHINE_RULES_RELATIVE
        if rules_path.is_symlink():
            raise ValueError("machine rules cannot be a symlink")
        rules = _load_object(rules_path)
        contract = _release_contract_view(
            rules.get("releaseManagementContract"), contract_root=package_dir
        )
        lock_name = contract["lockFileName"]
        lock_path = package_dir / lock_name
        if lock_path.is_symlink():
            raise ValueError("release lock cannot be a symlink")
        lock = _load_object(lock_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
        return None, [
            _runtime_contract()["errorCodes"]["invalidReleaseLock"]
        ]
    codes = contract["errorCodes"]
    lock_fields = contract["lockFields"]
    file_fields = contract["fileFields"]
    files = lock.get(lock_fields["files"])
    if not (
        set(lock) == set(lock_fields.values())
        and lock.get(lock_fields["artifactType"])
        == contract["lockArtifactType"]
        and lock.get(lock_fields["lockSchemaVersion"])
        == contract["lockSchemaVersion"]
        and lock.get(lock_fields["skillName"]) == "produce-meme-template"
        and isinstance(lock.get(lock_fields["skillVersion"]), str)
        and SEMVER_PATTERN.fullmatch(lock[lock_fields["skillVersion"]])
        and isinstance(lock.get(lock_fields["artifactSchemaVersion"]), str)
        and SEMVER_PATTERN.fullmatch(
            lock[lock_fields["artifactSchemaVersion"]]
        )
        and isinstance(lock.get(lock_fields["galleryContractVersion"]), str)
        and lock[lock_fields["galleryContractVersion"]].strip()
        and isinstance(lock.get(lock_fields["gitCommit"]), str)
        and re.fullmatch(r"[0-9a-f]{40}", lock[lock_fields["gitCommit"]])
        and (
            (
                isinstance(
                    lock.get(lock_fields["reviewComparisonBaseGitCommit"]),
                    str,
                )
                and re.fullmatch(
                    r"[0-9a-f]{40}",
                    lock[lock_fields["reviewComparisonBaseGitCommit"]],
                )
                and lock[lock_fields["reviewComparisonBaseGitCommit"]]
                != lock[lock_fields["gitCommit"]]
            )
            if int(lock[lock_fields["skillVersion"]].split(".", 1)[0]) >= 1
            else lock.get(lock_fields["reviewComparisonBaseGitCommit"]) is None
        )
        and isinstance(lock.get(lock_fields["builtAt"]), str)
        and lock[lock_fields["builtAt"]].strip()
        and isinstance(files, list)
        and all(
            isinstance(entry, dict)
            and set(entry) == set(file_fields.values())
            and isinstance(entry.get(file_fields["path"]), str)
            and _safe_relative_path(entry[file_fields["path"]])
            and isinstance(entry.get(file_fields["sha256"]), str)
            and re.fullmatch(
                r"[0-9a-f]{64}", entry[file_fields["sha256"]]
            )
            and isinstance(entry.get(file_fields["bytes"]), int)
            and not isinstance(entry.get(file_fields["bytes"]), bool)
            and entry[file_fields["bytes"]] >= 0
            for entry in files
        )
        and len(files)
        == len({entry[file_fields["path"]] for entry in files})
        and lock.get(lock_fields["contentDigest"])
        == _content_digest(files, lock_fields, file_fields)
        and lock.get(lock_fields["releaseDigest"])
        == _lock_digest(lock, lock_fields)
    ):
        return lock, [codes["invalidReleaseLock"]]
    expected = {entry[file_fields["path"]] for entry in files} | {lock_name}
    observed = _relative_files(package_dir, contract)
    if expected - observed:
        errors.append(codes["missingReleaseFile"])
    if observed - expected:
        errors.append(codes["extraInstallFile"])
    for entry in files:
        path = package_dir / entry[file_fields["path"]]
        if not path.is_file() or path.is_symlink():
            errors.append(codes["fileDigestMismatch"])
            continue
        if (
            path.stat().st_size != entry[file_fields["bytes"]]
            or _sha_file(path) != entry[file_fields["sha256"]]
        ):
            errors.append(codes["fileDigestMismatch"])
            break
    try:
        release, _, _ = _release_metadata(package_dir)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        errors.append(codes["mixedVersion"])
    else:
        if (
            release["skillVersion"] != lock.get(lock_fields["skillVersion"])
            or release["artifactSchemaVersion"]
            != lock.get(lock_fields["artifactSchemaVersion"])
            or release["supportedContracts"]["galleryTemplate"]
            != lock.get(lock_fields["galleryContractVersion"])
        ):
            errors.append(codes["mixedVersion"])
        gallery_path = package_dir / contract["gallerySnapshotRelativePath"]
        if (
            not gallery_path.is_file()
            or gallery_path.is_symlink()
            or _sha_file(gallery_path)
            != lock.get(lock_fields["galleryContractSha256"])
        ):
            errors.append(codes["mixedVersion"])
    if require_read_only and not _runtime_is_read_only(package_dir):
        errors.append(codes["mutableReleaseRuntime"])
    return lock, sorted(set(errors))


def promote_release(
    candidate_package: str | Path,
    dist_root: str | Path,
    *,
    readiness_root: str | Path,
) -> dict[str, Any]:
    candidate = Path(candidate_package).resolve()
    dist = Path(dist_root).resolve()
    lock, verification_errors = _verify_release(candidate)
    if lock is None or verification_errors:
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"]["invalidReleaseLock"],
            "verificationErrors": verification_errors,
        }
    rules = _load_object(candidate / MACHINE_RULES_RELATIVE)
    contract = _release_contract_view(
        rules.get("releaseManagementContract"), contract_root=candidate
    )
    codes = contract["errorCodes"]
    lock_fields = contract["lockFields"]
    file_fields = contract["fileFields"]
    version = lock[lock_fields["skillVersion"]]
    if int(version.split(".", 1)[0]) < 1:
        return {
            "pass": False,
            "errorCode": codes["invalidReleaseMetadata"],
            "message": "only stable releases use readiness promotion",
        }
    verifier_returncode: int | None = None
    try:
        if (
            candidate == dist
            or candidate.is_relative_to(dist)
            or dist.is_relative_to(candidate)
        ):
            return {
                "pass": False,
                "errorCode": codes["installPathConflict"],
                "message": "candidate and public dist roots must not overlap",
            }
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(candidate / "scripts" / "release_tool.py"),
                "verify-readiness",
                "--readiness",
                str(Path(readiness_root).absolute()),
                "--candidate",
                str(candidate),
                "--expected-release-digest",
                lock[lock_fields["releaseDigest"]],
                "--expected-git-commit",
                lock[lock_fields["gitCommit"]],
            ],
            cwd=candidate,
            capture_output=True,
            text=True,
            check=False,
            timeout=contract["validationTimeoutSeconds"],
        )
        verifier_returncode = completed.returncode
        readiness = json.loads(completed.stdout)
        if not isinstance(readiness, dict):
            readiness = {"pass": False}
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        readiness = {"pass": False}
    if not (
        verifier_returncode == 0
        and readiness.get("pass") is True
        and readiness.get("releaseDigest")
        == lock[lock_fields["releaseDigest"]]
        and readiness.get("gitCommit") == lock[lock_fields["gitCommit"]]
        and isinstance(readiness.get("reportPath"), str)
        and isinstance(readiness.get("completionPath"), str)
    ):
        return {
            "pass": False,
            "errorCode": codes["releaseReadinessRequired"],
            "message": "stable promotion requires a valid live readiness completion",
        }
    package = dist / lock[lock_fields["skillName"]] / version
    if package.exists():
        return {
            "pass": False,
            "errorCode": codes["releaseAlreadyExists"],
            "packageDir": str(package),
        }
    package.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{version}.", dir=package.parent)
    )
    try:
        for entry in lock[lock_fields["files"]]:
            relative = entry[file_fields["path"]]
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((candidate / relative).read_bytes())
        (staging / contract["lockFileName"]).write_bytes(
            (candidate / contract["lockFileName"]).read_bytes()
        )
        _make_runtime_read_only(staging)
        promoted_lock, promoted_errors = _verify_release(staging)
        if promoted_errors or promoted_lock != lock:
            raise OSError("promoted release verification failed")
        staging.rename(package)
    except OSError as exc:
        _discard_staging_tree(staging)
        return {
            "pass": False,
            "errorCode": codes["releaseValidationFailure"],
            "message": type(exc).__name__,
        }
    return {
        "pass": True,
        "candidate": False,
        "promoted": True,
        "packageDir": str(package),
        "releaseDigest": lock[lock_fields["releaseDigest"]],
        "contentDigest": lock[lock_fields["contentDigest"]],
        "skillVersion": version,
        "artifactSchemaVersion": lock[lock_fields["artifactSchemaVersion"]],
        "galleryContractVersion": lock[
            lock_fields["galleryContractVersion"]
        ],
        "readinessReportPath": readiness["reportPath"],
        "readinessCompletionPath": readiness["completionPath"],
    }


def install_release(
    package_dir: str | Path,
    install_root: str | Path,
    *,
    expected_release_digest: str,
) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    root = Path(install_root).resolve()
    bootstrap_codes = _runtime_contract()["errorCodes"]
    if (
        package == root
        or package in root.parents
        or root in package.parents
    ):
        return {
            "pass": False,
            "errorCode": bootstrap_codes["installPathConflict"],
        }
    lock, errors = _verify_release(package)
    if lock is None or errors:
        return {
            "pass": False,
            "errorCode": errors[0]
            if errors
            else _runtime_contract()["errorCodes"]["invalidReleaseLock"],
            "errorCodes": errors,
        }
    rules = _load_object(package / MACHINE_RULES_RELATIVE)
    release_contract = _release_contract_view(
        rules.get("releaseManagementContract"), contract_root=package
    )
    lock_fields = release_contract["lockFields"]
    if not (
        isinstance(expected_release_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_release_digest)
        and lock[lock_fields["releaseDigest"]] == expected_release_digest
    ):
        return {
            "pass": False,
            "errorCode": release_contract["errorCodes"][
                "releaseDigestMismatch"
            ],
        }
    version = lock[lock_fields["skillVersion"]]
    layout = release_contract["installLayout"]
    codes = release_contract["errorCodes"]
    target = root / layout["versionsDirectory"] / version
    target_ready = False
    if target.exists():
        target_lock, target_errors = _verify_release(target)
        target_fields = release_contract["lockFields"]
        target_ready = bool(
            target_lock is not None
            and not target_errors
            and target_lock[target_fields["releaseDigest"]]
            == lock[target_fields["releaseDigest"]]
        )
        if not target_ready:
            return {
                "pass": False,
                "errorCode": codes["installVersionExists"],
                "currentDir": str(target),
            }
    if not target_ready:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{version}.", dir=target.parent)
        )
        try:
            shutil.copytree(package, staging, dirs_exist_ok=True)
            verified_lock, install_errors = _verify_release(
                staging, require_read_only=False
            )
            if verified_lock is None or install_errors:
                return {
                    "pass": False,
                    "errorCode": install_errors[0],
                    "errorCodes": install_errors,
                }
            smoke_passed, smoke_detail = _run_release_validation(
                staging, release_contract, include_tests=False
            )
            if not smoke_passed:
                return {
                    "pass": False,
                    "errorCode": codes["releaseValidationFailure"],
                    "message": smoke_detail,
                }
            post_smoke_lock, post_smoke_errors = _verify_release(
                staging, require_read_only=False
            )
            if post_smoke_lock is None or post_smoke_errors:
                return {
                    "pass": False,
                    "errorCode": codes["releaseValidationFailure"],
                    "errorCodes": post_smoke_errors,
                }
            try:
                _make_runtime_read_only(staging)
            except OSError as exc:
                return {
                    "pass": False,
                    "errorCode": codes["releaseValidationFailure"],
                    "message": "install staging could not be frozen: "
                    + type(exc).__name__,
                }
            frozen_lock, frozen_errors = _verify_release(staging)
            if frozen_lock is None or frozen_errors:
                return {
                    "pass": False,
                    "errorCode": codes["releaseValidationFailure"],
                    "errorCodes": frozen_errors,
                }
            try:
                staging.rename(target)
            except OSError:
                if not target.exists():
                    raise
                raced_lock, raced_errors = _verify_release(target)
                if not (
                    raced_lock is not None
                    and not raced_errors
                    and raced_lock[lock_fields["releaseDigest"]]
                    == expected_release_digest
                ):
                    return {
                        "pass": False,
                        "errorCode": codes["installVersionExists"],
                    }
        finally:
            if staging.exists():
                _discard_staging_tree(staging)
    records = root / layout["recordsDirectory"]
    records.mkdir(parents=True, exist_ok=True)
    record_fields = release_contract["installRecordFields"]
    record = {
        record_fields["skillVersion"]: version,
        record_fields["releaseDigest"]: lock[
            lock_fields["releaseDigest"]
        ],
        record_fields["packageSource"]: str(package),
        record_fields["installedPath"]: str(target),
    }
    record_path = records / f"{version}.json"
    if record_path.exists():
        try:
            if _load_object(record_path) != record:
                return {
                    "pass": False,
                    "errorCode": codes["invalidInstallRecord"],
                }
        except (OSError, ValueError, json.JSONDecodeError):
            return {
                "pass": False,
                "errorCode": codes["invalidInstallRecord"],
            }
    else:
        try:
            with record_path.open("xb") as handle:
                handle.write(_json_bytes(record))
        except FileExistsError:
            try:
                if _load_object(record_path) != record:
                    return {
                        "pass": False,
                        "errorCode": codes["invalidInstallRecord"],
                    }
            except (OSError, ValueError, json.JSONDecodeError):
                return {
                    "pass": False,
                    "errorCode": codes["invalidInstallRecord"],
                }
    if target_ready:
        smoke_passed, smoke_detail = _run_release_validation(
            target, release_contract, include_tests=False
        )
        if not smoke_passed:
            return {
                "pass": False,
                "errorCode": codes["releaseValidationFailure"],
                "message": smoke_detail,
            }
        post_smoke_lock, post_smoke_errors = _verify_release(target)
        if post_smoke_lock is None or post_smoke_errors:
            return {
                "pass": False,
                "errorCode": codes["releaseValidationFailure"],
                "errorCodes": post_smoke_errors,
            }
    if not _runtime_is_read_only(target):
        return {
            "pass": False,
            "errorCode": codes["mutableReleaseRuntime"],
        }
    current = root / layout["currentPointer"]
    root.mkdir(parents=True, exist_ok=True)
    if current.exists() and not current.is_symlink():
        return {
            "pass": False,
            "errorCode": codes["installPointerConflict"],
        }
    link_staging = Path(tempfile.mkdtemp(prefix=".current-", dir=root))
    temporary_link = link_staging / "pointer"
    try:
        temporary_link.symlink_to(target, target_is_directory=True)
        temporary_link.replace(current)
    finally:
        shutil.rmtree(link_staging, ignore_errors=True)
    return {
        "pass": True,
        "currentDir": str(current),
        "installedDir": str(target),
        "releaseDigest": lock[lock_fields["releaseDigest"]],
    }


def _runtime_release_state(
    runtime: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[str], str]:
    bootstrap_contract = _runtime_contract()
    try:
        runtime_rules = _load_object(runtime / MACHINE_RULES_RELATIVE)
        release_contract = _release_contract_view(
            runtime_rules.get("releaseManagementContract"), contract_root=runtime
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
        release_contract = None
    declared_lock_name = (
        release_contract.get("lockFileName")
        if isinstance(release_contract, dict)
        else None
    )
    bootstrap_lock_name = bootstrap_contract["lockFileName"]
    release_lock_present = (runtime / bootstrap_lock_name).exists() or (
        isinstance(declared_lock_name, str)
        and (runtime / declared_lock_name).exists()
    )
    if release_lock_present:
        lock, errors = _verify_release(runtime)
        lock_fields = (
            release_contract["lockFields"]
            if release_contract is not None
            else bootstrap_contract["lockFields"]
        )
        install_source = (
            "unknown"
            if lock is None
            else "release-package:"
            + str(lock.get(lock_fields["releaseDigest"], "unknown"))
        )
        if lock is not None and release_contract is not None:
            layout = release_contract["installLayout"]
            version = lock[lock_fields["skillVersion"]]
            if (
                runtime.parent.name == layout["versionsDirectory"]
                and runtime.name == version
            ):
                record_fields = release_contract["installRecordFields"]
                record_path = (
                    runtime.parent.parent
                    / layout["recordsDirectory"]
                    / f"{version}.json"
                )
                try:
                    record = _load_object(record_path)
                    if not (
                        set(record) == set(record_fields.values())
                        and record[record_fields["skillVersion"]] == version
                        and record[record_fields["releaseDigest"]]
                        == lock[lock_fields["releaseDigest"]]
                        and record[record_fields["installedPath"]]
                        == str(runtime)
                        and isinstance(
                            record[record_fields["packageSource"]], str
                        )
                        and record[record_fields["packageSource"]]
                    ):
                        raise ValueError("invalid install record")
                    install_source = record[
                        record_fields["packageSource"]
                    ]
                except (
                    OSError,
                    TypeError,
                    ValueError,
                    KeyError,
                    json.JSONDecodeError,
                ):
                    errors.append(
                        release_contract["errorCodes"][
                            "invalidInstallRecord"
                        ]
                    )
    else:
        try:
            release, manifest, source_rules = _release_metadata(runtime)
            release_contract = _release_contract_view(
                source_rules.get("releaseManagementContract"), contract_root=runtime
            )
            lock_fields = release_contract["lockFields"]
            file_fields = release_contract["fileFields"]
            tracked_paths = [
                runtime / relative for relative in manifest["tracked_files"]
            ]
            if not all(
                path.is_file()
                and not path.is_symlink()
                and path.resolve().is_relative_to(runtime)
                for path in tracked_paths
            ):
                raise ValueError("source runtime contains an unsafe tracked file")
            files = [
                {
                    file_fields["path"]: relative,
                    file_fields["sha256"]: _sha_file(runtime / relative),
                    file_fields["bytes"]: (runtime / relative).stat().st_size,
                }
                for relative in manifest["tracked_files"]
            ]
            git_snapshot = _git_snapshot(runtime)
            if (
                git_snapshot is None
                or git_snapshot["files"]
                != set(manifest["tracked_files"])
            ):
                raise ValueError("source runtime is not its Git repository root")
            lock = {
                lock_fields["skillVersion"]: release["skillVersion"],
                lock_fields["artifactSchemaVersion"]: release["artifactSchemaVersion"],
                lock_fields["galleryContractVersion"]: release["supportedContracts"]["galleryTemplate"],
                lock_fields["contentDigest"]: _content_digest(
                    files, lock_fields, file_fields
                ),
                lock_fields["releaseDigest"]: None,
                lock_fields["gitCommit"]: git_snapshot["head"],
            }
            errors = []
            install_source = "source-worktree"
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            KeyError,
            subprocess.SubprocessError,
        ):
            lock = None
            errors = [
                release_contract["errorCodes"]["invalidSourceRuntime"]
                if release_contract is not None
                else _runtime_contract()["errorCodes"]["invalidSourceRuntime"]
            ]
            install_source = "unknown"
    return (
        release_contract or bootstrap_contract,
        lock,
        sorted(set(errors)),
        install_source,
    )


def _production_pin_for_state(
    runtime: Path,
    contract: dict[str, Any],
    lock: dict[str, Any],
) -> dict[str, Any]:
    rules = _load_object(runtime / MACHINE_RULES_RELATIVE)
    release, manifest, _ = _release_metadata(runtime)
    lock_fields = contract["lockFields"]
    pin_fields = contract["productionPinFields"]
    skill_fields = contract["productionPinSkillFields"]
    gallery_fields = contract["productionPinGalleryFields"]
    gallery_path = contract["gallerySnapshotRelativePath"]
    gallery_metadata = _load_object(
        runtime / contract["gallerySnapshotMetadataRelativePath"]
    )
    gallery_source_sha = gallery_metadata.get("sourceArtifactSha256")
    if (
        gallery_metadata.get("contractId") != "gallery-template"
        or gallery_metadata.get("contractVersion")
        != release["supportedContracts"]["galleryTemplate"]
        or gallery_metadata.get("schemaFile") != Path(gallery_path).name
        or not isinstance(gallery_source_sha, str)
        or not re.fullmatch(r"[0-9a-f]{64}", gallery_source_sha)
        or gallery_source_sha != _sha_file(runtime / gallery_path)
    ):
        raise ValueError("gallery contract snapshot metadata is invalid")
    return {
        pin_fields["artifactType"]: contract["productionPinArtifactType"],
        pin_fields["schemaVersion"]: rules["schemaVersion"],
        pin_fields["skill"]: {
            skill_fields["name"]: release["skillName"],
            skill_fields["version"]: lock[lock_fields["skillVersion"]],
        },
        pin_fields["artifactSchemaVersion"]: lock[
            lock_fields["artifactSchemaVersion"]
        ],
        pin_fields["releaseSha256"]: lock[lock_fields["contentDigest"]],
        pin_fields["releaseManifestSha256"]: _sha_file(
            runtime / "skill-manifest.json"
        ),
        pin_fields["releaseFileCount"]: len(manifest["tracked_files"]),
        pin_fields["machineRulesSha256"]: _sha_file(
            runtime / MACHINE_RULES_RELATIVE
        ),
        pin_fields["gitCommit"]: lock[lock_fields["gitCommit"]],
        pin_fields["validatorSha256"]: _sha_file(
            runtime / contract["validatorRelativePath"]
        ),
        pin_fields["replacementSpecVersion"]: contract[
            "replacementSpecVersion"
        ],
        pin_fields["replacementSpecSha256"]: _sha_file(
            runtime / contract["replacementSpecRelativePath"]
        ),
        pin_fields["galleryContract"]: {
            gallery_fields["id"]: lock[
                lock_fields["galleryContractVersion"]
            ],
            gallery_fields["snapshot"]: gallery_path,
            gallery_fields["sha256"]: _sha_file(runtime / gallery_path),
            gallery_fields["upstreamSourceSha256"]: gallery_source_sha,
        },
    }


def runtime_production_pin(runtime_root: str | Path) -> dict[str, Any]:
    runtime = Path(runtime_root).resolve()
    contract, lock, errors, _ = _runtime_release_state(runtime)
    if lock is None or errors:
        raise ValueError("runtime release identity is invalid")
    return _production_pin_for_state(runtime, contract, lock)


def runtime_production_pin_sha256(runtime_root: str | Path) -> str:
    """Return the digest of the pin bytes persisted by the production seam."""

    return _sha_bytes(
        _pretty_json_bytes(runtime_production_pin(runtime_root))
    )


def doctor(
    runtime_root: str | Path,
    *,
    production_pin: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root).resolve()
    bootstrap_contract = _runtime_contract()
    release_contract, lock, errors, install_source = _runtime_release_state(
        runtime
    )
    if lock is None:
        diagnostic_fields = bootstrap_contract["diagnosticFields"]
        return {
            "pass": False,
            diagnostic_fields["runtimeRoot"]: str(runtime),
            diagnostic_fields["installSource"]: install_source,
            diagnostic_fields["errorCodes"]: errors,
            diagnostic_fields[
                "remediation"
            ]: "reinstall from a verified release package",
        }
    lock_fields = release_contract["lockFields"]
    diagnostic_fields = release_contract["diagnosticFields"]
    try:
        expected_pin = (
            None
            if errors
            else _production_pin_for_state(runtime, release_contract, lock)
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        expected_pin = None
        errors.append(release_contract["errorCodes"]["mixedVersion"])
    if production_pin is not None:
        if not _production_pin_shape_valid(
            production_pin, release_contract
        ):
            errors.append(
                release_contract["errorCodes"]["invalidProductionPin"]
            )
        elif expected_pin is not None and production_pin != expected_pin:
            errors.append(
                release_contract["errorCodes"]["productionPinMismatch"]
            )
    return {
        "pass": not errors,
        diagnostic_fields["runtimeRoot"]: str(runtime),
        diagnostic_fields["installSource"]: install_source,
        diagnostic_fields["skillVersion"]: lock.get(
            lock_fields["skillVersion"]
        ),
        diagnostic_fields["artifactSchemaVersion"]: lock.get(
            lock_fields["artifactSchemaVersion"]
        ),
        diagnostic_fields["galleryContractVersion"]: lock.get(
            lock_fields["galleryContractVersion"]
        ),
        diagnostic_fields["releaseDigest"]: lock.get(
            lock_fields["contentDigest"]
        ),
        diagnostic_fields["releaseLockSha256"]: lock.get(
            lock_fields["releaseDigest"]
        ),
        diagnostic_fields["productionPin"]: expected_pin,
        diagnostic_fields["errorCodes"]: sorted(set(errors)),
        diagnostic_fields["remediation"]: (
            None if not errors else "reinstall or resume with the pinned release"
        ),
    }


def write_pin_migration_report(
    production_item_dir: str | Path,
    new_runtime_root: str | Path,
) -> dict[str, Any]:
    item_dir = Path(production_item_dir).resolve()
    pin_path = item_dir / "production-pin.json"
    if (
        not pin_path.is_file()
        or pin_path.is_symlink()
        or not pin_path.resolve().is_relative_to(item_dir)
    ):
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"][
                "invalidProductionPin"
            ],
        }
    try:
        old_pin = _load_object(pin_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"][
                "invalidProductionPin"
            ],
        }
    if not _production_pin_shape_valid(
        old_pin, _runtime_contract(), allow_legacy=True
    ):
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"][
                "invalidProductionPin"
            ],
        }
    manifest_path = item_dir / "production-manifest.json"
    try:
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or not manifest_path.resolve().is_relative_to(item_dir)
        ):
            raise ValueError("unsafe production manifest")
        production_manifest = _load_object(manifest_path)
        from .workflow_core import validate_production_manifest_lineage

        lineage_errors = validate_production_manifest_lineage(
            item_dir, production_manifest
        )
        if lineage_errors:
            raise ValueError("invalid production manifest lineage")
        production_item_id = production_manifest.get("productionItemId")
        template_revision = production_manifest.get("revision")
        artifacts = production_manifest.get("artifacts")
        pin_record = (
            artifacts.get("production-pin.json")
            if isinstance(artifacts, dict)
            else None
        )
        if not (
            isinstance(production_item_id, str)
            and production_item_id.strip()
            and isinstance(template_revision, int)
            and not isinstance(template_revision, bool)
            and template_revision > 0
            and isinstance(production_manifest.get("state"), str)
            and production_manifest["state"].strip()
            and isinstance(pin_record, dict)
            and pin_record.get("sha256") == _sha_file(pin_path)
            and pin_record.get("bytes") == pin_path.stat().st_size
        ):
            raise ValueError("invalid production manifest identity")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"][
                "invalidProductionManifest"
            ],
        }
    diagnosis = doctor(new_runtime_root)
    if not diagnosis["pass"]:
        return {
            "pass": False,
            "errorCode": _runtime_contract()["errorCodes"][
                "newRuntimeInvalid"
            ],
            "doctor": diagnosis,
        }
    runtime_rules = _load_object(
        Path(new_runtime_root).resolve() / MACHINE_RULES_RELATIVE
    )
    release_contract = _release_contract_view(
        runtime_rules.get("releaseManagementContract"),
        contract_root=Path(new_runtime_root).resolve(),
    )
    new_pin = runtime_production_pin(new_runtime_root)
    codes = release_contract["errorCodes"]
    migration_fields = release_contract["migrationFields"]
    pin_fields = release_contract["productionPinFields"]
    skill_fields = release_contract["productionPinSkillFields"]
    gallery_fields = release_contract["productionPinGalleryFields"]
    migration_pattern = release_contract["migrationFilePattern"]
    migration_regex = re.compile(
        re.escape(migration_pattern).replace(
            re.escape("{revision}"), r"(\d+)"
        )
    )
    revisions = [
        int(match.group(1))
        for path in item_dir.iterdir()
        if (match := migration_regex.fullmatch(path.name))
    ]
    revision = max(revisions, default=0) + 1
    report_path = item_dir / migration_pattern.format(revision=revision)
    report = {
        migration_fields["artifactType"]: release_contract[
            "migrationArtifactType"
        ],
        migration_fields["revision"]: revision,
        migration_fields["oldPinSha256"]: _sha_file(pin_path),
        migration_fields["newPinSha256"]: _sha_bytes(_json_bytes(new_pin)),
        migration_fields["oldPin"]: old_pin,
        migration_fields["newPin"]: new_pin,
        migration_fields["productionItemId"]: production_item_id,
        migration_fields["templateRevision"]: template_revision,
        migration_fields["productionManifestSha256"]: _sha_file(
            manifest_path
        ),
        migration_fields["changedVersionLines"]: [
            role
            for role, old_value, new_value in (
                (
                    "skillVersion",
                    old_pin[pin_fields["skill"]][skill_fields["version"]],
                    new_pin[pin_fields["skill"]][skill_fields["version"]],
                ),
                (
                    "artifactSchemaVersion",
                    old_pin[pin_fields["artifactSchemaVersion"]],
                    new_pin[pin_fields["artifactSchemaVersion"]],
                ),
                (
                    "galleryContractVersion",
                    old_pin[pin_fields["galleryContract"]][
                        gallery_fields["id"]
                    ],
                    new_pin[pin_fields["galleryContract"]][
                        gallery_fields["id"]
                    ],
                ),
                (
                    "releaseDigest",
                    old_pin[pin_fields["releaseSha256"]],
                    new_pin[pin_fields["releaseSha256"]],
                ),
                (
                    "replacementSpecVersion",
                    old_pin.get(pin_fields["replacementSpecVersion"]),
                    new_pin[pin_fields["replacementSpecVersion"]],
                ),
            )
            if old_value != new_value
        ],
        migration_fields["invalidateFromPhase"]: runtime_rules[
            "productionPhases"
        ][
            release_contract["migrationInvalidateFromPhaseIndex"]
        ]["phase"],
    }
    try:
        with report_path.open("xb") as handle:
            handle.write(_json_bytes(report))
    except FileExistsError:
        return {"pass": False, "errorCode": codes["immutableMigrationConflict"]}
    return {"pass": True, "reportPath": str(report_path), "revision": revision}
