from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
import io
from unittest import mock
from pathlib import Path

from scripts.produce_meme_template.experience_regression import (
    EvidenceStatusRecordingResult,
    ExperienceRegressionAdapters,
    compile_evidence_execution_results,
    run_experience_regression,
)
from scripts.produce_meme_template.release_management import (
    build_release,
    doctor,
    install_release,
    runtime_production_pin,
)
from scripts.release_validation_runner import (
    CompletedSuiteEvidenceAdapters,
)
from tests.test_issue_13_release_doctor_install import (
    BUILT_AT,
    commit_source,
    copy_release_source,
    prepare_development_source,
)


ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
CONTRACT = RULES["historicalExperienceContract"]
FIELDS = CONTRACT["reportFields"]
ENTRY_FIELDS = CONTRACT["reportExperienceFields"]
CORPUS_FIELDS = CONTRACT["reportCorpusFields"]
OUTCOMES = CONTRACT["outcomes"]
EVIDENCE_OUTCOMES = CONTRACT["evidenceOutcomes"]
RESULT_FIELDS = CONTRACT["evidenceResultFields"]
FAILURES = CONTRACT["failureCategories"]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class PassingRegressionAdapters(ExperienceRegressionAdapters):
    def runtime_pin(self, runtime_root: Path) -> dict:
        return runtime_production_pin(runtime_root)

    def execute_evidence(
        self, runtime_root: Path, test_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        return compile_evidence_execution_results(
            runtime_root,
            test_ids,
            {test_id: "passed" for test_id in test_ids},
        )


class UnavailableRegressionAdapters(PassingRegressionAdapters):
    def execute_evidence(
        self, runtime_root: Path, test_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        del runtime_root
        return {
            test_id: {
                RESULT_FIELDS["outcome"]: EVIDENCE_OUTCOMES["unavailable"],
                RESULT_FIELDS["detail"]: "adapter offline",
                RESULT_FIELDS["observedGateLocator"]: None,
                RESULT_FIELDS["observedGateValue"]: None,
            }
            for test_id in test_ids
        }


class FailingRegressionAdapters(PassingRegressionAdapters):
    def execute_evidence(
        self, runtime_root: Path, test_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        del runtime_root
        return {
            test_id: {
                RESULT_FIELDS["outcome"]: EVIDENCE_OUTCOMES["failed"],
                RESULT_FIELDS["detail"]: "assertion failed",
                RESULT_FIELDS["observedGateLocator"]: None,
                RESULT_FIELDS["observedGateValue"]: None,
            }
            for test_id in test_ids
        }


class DriftedRegressionAdapters(PassingRegressionAdapters):
    def runtime_pin(self, runtime_root: Path) -> dict:
        pin = super().runtime_pin(runtime_root)
        fields = RULES["releaseManagementContract"]["productionPinFields"]
        pin[fields["machineRulesSha256"]] = "0" * 64
        return pin


class PassingInstalledRuntimeAdapters(PassingRegressionAdapters):
    pass


class MalformedFormalResultAdapters(PassingRegressionAdapters):
    def formal_contract_valid(
        self, runtime_root: Path, template: dict
    ) -> bool:
        del runtime_root, template
        return {"pass": True}  # type: ignore[return-value]


class RaisingFormalResultAdapters(PassingRegressionAdapters):
    def formal_contract_valid(
        self, runtime_root: Path, template: dict
    ) -> bool:
        del runtime_root, template
        raise TypeError("malformed formal adapter response")


class Issue15HistoricalExperienceRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.copy_index = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_audit(
        self,
        runtime_root: Path = ROOT,
        adapters: ExperienceRegressionAdapters | None = None,
        *,
        write_report: bool = True,
    ) -> dict:
        return run_experience_regression(
            runtime_root,
            (
                self.root / "historical-experience-regression.json"
                if write_report
                else None
            ),
            adapters=adapters or PassingRegressionAdapters(),
        )

    def copy_runtime(self) -> Path:
        self.copy_index += 1
        destination = self.root / f"runtime-{self.copy_index}"
        shutil.copytree(
            ROOT,
            destination,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        commit_source(destination)
        return destination

    def test_e01_e39_each_bind_one_rule_behavior_and_executable_evidence(
        self,
    ) -> None:
        report = self.run_audit()

        self.assertTrue(report[FIELDS["pass"]])
        self.assertEqual(OUTCOMES["passed"], report[FIELDS["outcome"]])
        experiences = report[FIELDS["experiences"]]
        self.assertEqual(CONTRACT["experienceIds"], [
            item[ENTRY_FIELDS["experienceId"]] for item in experiences
        ])
        self.assertTrue(
            all(item[ENTRY_FIELDS["authority"]] for item in experiences)
        )
        self.assertTrue(
            all(item[ENTRY_FIELDS["implementation"]] for item in experiences)
        )
        self.assertTrue(
            all(item[ENTRY_FIELDS["evidence"]] for item in experiences)
        )
        evidence_fields = CONTRACT["reportEvidenceFields"]
        self.assertEqual(
            set(CONTRACT["evidencePolarities"].values()),
            {
                evidence[evidence_fields["polarity"]]
                for item in experiences
                for evidence in item[ENTRY_FIELDS["evidence"]]
            },
        )

    def test_latest_samples_and_five_representative_case_families_are_frozen(
        self,
    ) -> None:
        report = self.run_audit()

        corpus = report[FIELDS["corpus"]]
        self.assertEqual(
            set(CONTRACT["requiredCorpusRoles"].values()), set(corpus)
        )
        self.assertTrue(
            all(item[CORPUS_FIELDS["pass"]] for item in corpus.values())
        )

    def test_representative_corpus_roles_cannot_be_spoofed_by_unrelated_json(
        self,
    ) -> None:
        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        corpus = matrix[CONTRACT["matrixFields"]["corpus"]]
        rules_path = runtime / "contracts" / "machine-rules.json"
        rules_sha = hashlib.sha256(rules_path.read_bytes()).hexdigest()
        for role in ("ordinaryPerson", "knownCharacterIp"):
            corpus_role = CONTRACT["requiredCorpusRoles"][role]
            corpus[corpus_role][CONTRACT["corpusFields"]["path"]] = (
                "contracts/machine-rules.json"
            )
            corpus[corpus_role][CONTRACT["corpusFields"]["sha256"]] = rules_sha
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

        report = self.run_audit(runtime, write_report=False)

        self.assertFalse(report[FIELDS["pass"]])
        self.assertIn(
            FAILURES["fixtureMissing"], report[FIELDS["failureCategories"]]
        )

    def test_bad_and_human_review_evidence_bind_a_machine_gate(self) -> None:
        report = self.run_audit(write_report=False)
        evidence_fields = CONTRACT["reportEvidenceFields"]
        good = CONTRACT["evidencePolarities"]["goodCase"]
        for experience in report[FIELDS["experiences"]]:
            for evidence in experience[ENTRY_FIELDS["evidence"]]:
                if evidence[evidence_fields["polarity"]] == good:
                    continue
                self.assertTrue(
                    evidence[evidence_fields["expectedGateLocator"]]
                )
                self.assertIsNotNone(
                    evidence[evidence_fields["expectedGateValue"]]
                )

        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        evidence_fields = CONTRACT["evidenceFields"]
        first_evidence = matrix[CONTRACT["matrixFields"]["experiences"]][0][
            CONTRACT["experienceFields"]["evidence"]
        ][0]
        first_evidence[evidence_fields["polarity"]] = good
        first_evidence.pop(evidence_fields["expectedGateLocator"])
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        relabeled = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["ruleMissing"],
            relabeled[FIELDS["failureCategories"]],
        )

        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        experiences = matrix[CONTRACT["matrixFields"]["experiences"]]
        first = experiences[0][CONTRACT["experienceFields"]["evidence"]][0]
        unrelated = experiences[10][CONTRACT["experienceFields"]["evidence"]][0]
        first[evidence_fields["testId"]] = unrelated[evidence_fields["testId"]]
        first[evidence_fields["fixturePaths"]] = unrelated[
            evidence_fields["fixturePaths"]
        ]
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        crossed = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["ruleMissing"], crossed[FIELDS["failureCategories"]]
        )

    def test_experience_authority_and_implementation_cannot_cross_bind(self) -> None:
        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        experience_fields = CONTRACT["experienceFields"]
        experiences = matrix[CONTRACT["matrixFields"]["experiences"]]
        experiences[0][experience_fields["authority"]] = copy.deepcopy(
            experiences[10][experience_fields["authority"]]
        )
        experiences[0][experience_fields["implementation"]] = copy.deepcopy(
            experiences[10][experience_fields["implementation"]]
        )
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

        report = self.run_audit(runtime, write_report=False)

        self.assertFalse(report[FIELDS["pass"]])
        self.assertIn(
            FAILURES["ruleMissing"], report[FIELDS["failureCategories"]]
        )

    def test_retired_capabilities_have_no_repository_entry_points(self) -> None:
        tracked = json.loads(
            (ROOT / "skill-manifest.json").read_text(encoding="utf-8")
        )["tracked_files"]
        for prefix in CONTRACT["retiredRepositoryPrefixes"]:
            self.assertFalse(
                any(path == prefix or path.startswith(prefix + "/") for path in tracked),
                prefix,
            )

    def test_missing_rule_and_fixture_have_distinct_machine_failures(self) -> None:
        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        experience_fields = CONTRACT["experienceFields"]
        authority_fields = CONTRACT["authorityFields"]
        evidence_fields = CONTRACT["evidenceFields"]
        experiences = matrix[CONTRACT["matrixFields"]["experiences"]]

        missing_rule = copy.deepcopy(matrix)
        missing_rule[CONTRACT["matrixFields"]["experiences"]][0][
            experience_fields["authority"]
        ][authority_fields["path"]] = "references/missing-rule.md"
        matrix_path.write_text(
            json.dumps(missing_rule, ensure_ascii=False), encoding="utf-8"
        )
        rule_report = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["ruleMissing"], rule_report[FIELDS["failureCategories"]]
        )

        fixture_missing = copy.deepcopy(matrix)
        fixture_missing[CONTRACT["matrixFields"]["experiences"]][0][
            experience_fields["evidence"]
        ][0][evidence_fields["fixturePaths"]] = ["fixtures/missing.json"]
        matrix_path.write_text(
            json.dumps(fixture_missing, ensure_ascii=False), encoding="utf-8"
        )
        fixture_report = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["fixtureMissing"],
            fixture_report[FIELDS["failureCategories"]],
        )

    def test_adapter_unavailable_and_formal_contract_mismatch_are_distinct(
        self,
    ) -> None:
        unavailable = self.run_audit(
            adapters=UnavailableRegressionAdapters(), write_report=False
        )
        self.assertIn(
            FAILURES["externalAdapterUnavailable"],
            unavailable[FIELDS["failureCategories"]],
        )

        runtime = self.copy_runtime()
        sample = (
            runtime
            / "fixtures"
            / "contracts"
            / "latest-gallery-samples"
            / "heart.expected.json"
        )
        record = json.loads(sample.read_text(encoding="utf-8"))
        record["coverUrl"] = record["cover"]
        sample.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        incompatible = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["formalContractIncompatible"],
            incompatible[FIELDS["failureCategories"]],
        )

        for adapters in (
            MalformedFormalResultAdapters(),
            RaisingFormalResultAdapters(),
        ):
            malformed = self.run_audit(adapters=adapters, write_report=False)
            self.assertFalse(malformed[FIELDS["pass"]])
            self.assertIn(
                FAILURES["externalAdapterUnavailable"],
                malformed[FIELDS["failureCategories"]],
            )

    def test_evidence_failure_and_version_drift_are_distinct(self) -> None:
        evidence_failure = self.run_audit(
            adapters=FailingRegressionAdapters(), write_report=False
        )
        self.assertIn(
            FAILURES["evidenceFailure"],
            evidence_failure[FIELDS["failureCategories"]],
        )

        version_drift = self.run_audit(
            adapters=DriftedRegressionAdapters(), write_report=False
        )
        self.assertIn(
            FAILURES["versionDrift"],
            version_drift[FIELDS["failureCategories"]],
        )

    def test_missing_experience_prevents_a_pass_report(self) -> None:
        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        experiences_field = CONTRACT["matrixFields"]["experiences"]
        matrix[experiences_field] = matrix[experiences_field][:-1]
        matrix_path.write_text(
            json.dumps(matrix, ensure_ascii=False), encoding="utf-8"
        )

        report = self.run_audit(runtime, write_report=False)

        self.assertFalse(report[FIELDS["pass"]])
        self.assertIn(
            FAILURES["ruleMissing"], report[FIELDS["failureCategories"]]
        )

    def test_malformed_nested_values_and_extra_ids_return_stable_failures(
        self,
    ) -> None:
        experience_fields = CONTRACT["experienceFields"]
        evidence_fields = CONTRACT["evidenceFields"]
        for mutation in ("fixture", "migration"):
            runtime = self.copy_runtime()
            matrix_path = runtime / CONTRACT["matrixRelativePath"]
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            first = matrix[CONTRACT["matrixFields"]["experiences"]][0]
            if mutation == "fixture":
                first[experience_fields["evidence"]][0][
                    evidence_fields["fixturePaths"]
                ] = [{}]
            else:
                first[experience_fields["migrationStatus"]] = {}
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            report = self.run_audit(runtime, write_report=False)
            self.assertFalse(report[FIELDS["pass"]])

        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        extra = copy.deepcopy(
            matrix[CONTRACT["matrixFields"]["experiences"]][-1]
        )
        extra[experience_fields["experienceId"]] = "E40"
        matrix[CONTRACT["matrixFields"]["experiences"]].append(extra)
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        report = self.run_audit(runtime, write_report=False)
        summary = report[FIELDS["summary"]]
        summary_fields = CONTRACT["summaryFields"]
        self.assertEqual(39, summary[summary_fields["total"]])
        self.assertEqual(
            summary[summary_fields["total"]],
            summary[summary_fields["passed"]] + summary[summary_fields["failed"]],
        )

    def test_empty_legacy_disposition_and_missing_matrix_fail_with_categories(
        self,
    ) -> None:
        runtime = self.copy_runtime()
        matrix_path = runtime / CONTRACT["matrixRelativePath"]
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        fields = CONTRACT["experienceFields"]
        matrix[CONTRACT["matrixFields"]["experiences"]][2][
            fields["legacyDisposition"]
        ] = "   "
        matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
        disposition = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["ruleMissing"], disposition[FIELDS["failureCategories"]]
        )

        runtime = self.copy_runtime()
        (runtime / CONTRACT["matrixRelativePath"]).unlink()
        missing = self.run_audit(runtime, write_report=False)
        self.assertIn(
            FAILURES["fixtureMissing"], missing[FIELDS["failureCategories"]]
        )

    def test_runtime_pin_is_exact_and_rechecked_after_adapter_execution(self) -> None:
        runtime_a = self.copy_runtime()
        pin_a = runtime_production_pin(runtime_a)
        runtime_b = self.root / "runtime-b"
        shutil.copytree(runtime_a, runtime_b, ignore=shutil.ignore_patterns(".git"))
        commit_source(runtime_b)
        requirements = runtime_b / "requirements.txt"
        requirements.write_text(
            requirements.read_text(encoding="utf-8") + "\n# drift\n",
            encoding="utf-8",
        )

        class ForeignPinAdapters(PassingRegressionAdapters):
            def runtime_pin(self, runtime_root: Path) -> dict:
                del runtime_root
                return pin_a

        foreign = self.run_audit(
            runtime_b, ForeignPinAdapters(), write_report=False
        )
        self.assertIn(
            FAILURES["versionDrift"], foreign[FIELDS["failureCategories"]]
        )

        runtime = self.copy_runtime()

        class MutatingAdapters(PassingRegressionAdapters):
            def execute_evidence(self, runtime_root: Path, test_ids: list[str]) -> dict:
                requirements_path = runtime_root / "requirements.txt"
                requirements_path.write_text(
                    requirements_path.read_text(encoding="utf-8") + "\n# changed\n",
                    encoding="utf-8",
                )
                return super().execute_evidence(runtime_root, test_ids)

        mutated = self.run_audit(
            runtime, MutatingAdapters(), write_report=False
        )
        self.assertIn(
            FAILURES["versionDrift"], mutated[FIELDS["failureCategories"]]
        )

        runtime = self.copy_runtime()
        (runtime / "skill-manifest.json").unlink()
        missing_manifest = self.run_audit(runtime, write_report=False)
        self.assertFalse(missing_manifest[FIELDS["pass"]])
        self.assertIsNone(missing_manifest[FIELDS["releaseManifestSha256"]])
        self.assertIn(
            FAILURES["versionDrift"],
            missing_manifest[FIELDS["failureCategories"]],
        )

    def test_skipped_evidence_is_unavailable_and_report_symlinks_are_rejected(
        self,
    ) -> None:
        class SkippedEvidence(unittest.TestCase):
            @unittest.skip("adapter unavailable")
            def runTest(self) -> None:
                pass

        case = SkippedEvidence()
        result = unittest.TextTestRunner(
            stream=io.StringIO(),
            resultclass=EvidenceStatusRecordingResult,
        ).run(unittest.TestSuite([case]))
        evidence = CompletedSuiteEvidenceAdapters(
            result.evidence_status_by_test_id
        ).execute_evidence(ROOT, [case.id()])
        self.assertEqual(
            EVIDENCE_OUTCOMES["unavailable"],
            evidence[case.id()][RESULT_FIELDS["outcome"]],
        )

        report = self.run_audit()
        report_path = self.root / "historical-experience-regression.json"
        external = self.root / "external-report.json"
        report_path.replace(external)
        report_path.symlink_to(external)
        with self.assertRaises(ValueError):
            run_experience_regression(
                ROOT, report_path, adapters=PassingRegressionAdapters()
            )
        real = self.root / "real" / "nested"
        real.mkdir(parents=True)
        alias = self.root / "alias"
        alias.symlink_to(self.root / "real", target_is_directory=True)
        with self.assertRaises(ValueError):
            run_experience_regression(
                ROOT,
                alias / "nested" / "report.json",
                adapters=PassingRegressionAdapters(),
            )
        atomic_path = self.root / "atomic-report.json"
        with mock.patch(
            "scripts.produce_meme_template.experience_regression.os.fdopen",
            side_effect=OSError("interrupted"),
        ):
            with self.assertRaises(OSError):
                run_experience_regression(
                    ROOT,
                    atomic_path,
                    adapters=PassingRegressionAdapters(),
                )
        self.assertFalse(atomic_path.exists())
        recovered = run_experience_regression(
            ROOT, atomic_path, adapters=PassingRegressionAdapters()
        )
        self.assertTrue(recovered[FIELDS["pass"]])
        self.assertTrue(report[FIELDS["pass"]])

    def test_rejected_report_paths_have_no_runtime_or_parent_side_effects(
        self,
    ) -> None:
        runtime = self.copy_runtime()
        inside_parent = runtime / "created-by-rejected-output"
        with self.assertRaises(ValueError):
            run_experience_regression(
                runtime,
                inside_parent / "report.json",
                adapters=PassingRegressionAdapters(),
            )
        self.assertFalse(inside_parent.exists())

        outside_parent = self.root / "outside-created"
        traversed_runtime_child = runtime / "created-through-dotdot"
        traversal = (
            outside_parent
            / ".."
            / runtime.name
            / traversed_runtime_child.name
            / "report.json"
        )
        with self.assertRaises(ValueError):
            run_experience_regression(
                runtime,
                traversal,
                adapters=PassingRegressionAdapters(),
            )
        self.assertFalse(outside_parent.exists())
        self.assertFalse(traversed_runtime_child.exists())

    def test_report_binds_runtime_pin_manifest_rules_matrix_and_is_create_once(
        self,
    ) -> None:
        report = self.run_audit()
        report_path = self.root / "historical-experience-regression.json"

        self.assertEqual(
            hashlib.sha256(canonical_bytes(report[FIELDS["runtimePin"]])).hexdigest(),
            report[FIELDS["runtimePinSha256"]],
        )
        self.assertEqual(
            hashlib.sha256((ROOT / "skill-manifest.json").read_bytes()).hexdigest(),
            report[FIELDS["releaseManifestSha256"]],
        )
        self.assertEqual(
            hashlib.sha256((ROOT / "contracts" / "machine-rules.json").read_bytes()).hexdigest(),
            report[FIELDS["machineRulesSha256"]],
        )
        self.assertEqual(
            hashlib.sha256(
                (ROOT / CONTRACT["matrixRelativePath"]).read_bytes()
            ).hexdigest(),
            report[FIELDS["matrixSha256"]],
        )
        self.assertEqual(report, json.loads(report_path.read_text(encoding="utf-8")))
        self.assertEqual(report, self.run_audit())

    def test_rewritten_conditional_and_retired_rules_have_explicit_disposition(
        self,
    ) -> None:
        matrix = json.loads(
            (ROOT / CONTRACT["matrixRelativePath"]).read_text(encoding="utf-8")
        )
        experience_fields = CONTRACT["experienceFields"]
        statuses = CONTRACT["migrationStatuses"]
        closed_roles = {"conditional", "rewritten", "retired"}
        closed_values = {statuses[role] for role in closed_roles}
        for experience in matrix[CONTRACT["matrixFields"]["experiences"]]:
            if experience[experience_fields["migrationStatus"]] in closed_values:
                self.assertTrue(
                    experience[experience_fields["legacyDisposition"]]
                )

    def test_installed_runtime_pin_and_regression_report_share_one_revision(
        self,
    ) -> None:
        source = copy_release_source(self.root / "source")
        prepare_development_source(source)
        git_commit = commit_source(source)
        with mock.patch(
            "scripts.produce_meme_template.release_management._run_release_validation",
            return_value=(True, None),
        ):
            built = build_release(
                source,
                self.root / "dist",
                git_commit=git_commit,
                built_at=BUILT_AT,
            )
        package = Path(built["packageDir"])
        installed = install_release(
            package,
            self.root / "install",
            expected_release_digest=built["releaseDigest"],
        )
        self.assertTrue(installed["pass"])
        current = Path(installed["currentDir"])
        report = run_experience_regression(
            current,
            self.root / "installed-regression.json",
            adapters=PassingInstalledRuntimeAdapters(),
        )

        pin = report[FIELDS["runtimePin"]]
        diagnostic = doctor(current, production_pin=pin)
        self.assertTrue(report[FIELDS["pass"]])
        self.assertTrue(diagnostic["pass"])
        release_contract = json.loads(
            (current / "contracts" / "machine-rules.json").read_text(
                encoding="utf-8"
            )
        )["releaseManagementContract"]
        pin_fields = release_contract["productionPinFields"]
        self.assertEqual(git_commit, pin[pin_fields["gitCommit"]])
        self.assertEqual(
            report[FIELDS["releaseManifestSha256"]],
            pin[pin_fields["releaseManifestSha256"]],
        )


if __name__ == "__main__":
    unittest.main()
