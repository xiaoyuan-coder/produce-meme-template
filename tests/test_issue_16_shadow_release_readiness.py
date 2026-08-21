from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts.produce_meme_template import (
    DeterministicFixtureAdapters,
    LiveShadowReadinessAdapters,
    RecordedShadowReadinessAdapters,
    live_release_readiness_preflight,
    live_shadow_request,
    recorded_shadow_request,
    run_production,
    run_release_readiness,
    verify_code_review_receipt,
    verify_release_readiness_completion,
)
from scripts.produce_meme_template.release_management import (
    runtime_production_pin_sha256,
)
from scripts.produce_meme_template import release_readiness as readiness_module


ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
CONTRACT = RULES["releaseReadinessContract"]
REPORT_FIELDS = CONTRACT["reportFields"]
ERROR_CODES = CONTRACT["errorCodes"]
SCENARIO_FIELDS = CONTRACT["scenarioFields"]
SCENARIO_REPORT_FIELDS = CONTRACT["scenarioReportFields"]
BASE_FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
SHADOW_FIXTURE = ROOT / "fixtures" / "shadow-release"
CORPUS = json.loads((SHADOW_FIXTURE / "corpus.json").read_text(encoding="utf-8"))


class NeverCalledReadinessAdapters:
    def workflow_adapters_for_scenario(self, scenario: dict):
        raise AssertionError("invalid readiness input cannot start production")


class NeverCalledLiveReviewer:
    def __init__(self) -> None:
        setattr(
            self,
            CONTRACT["liveReviewAdapterFields"]["methodIdentity"],
            CONTRACT["liveReviewMethodIds"][0],
        )


class RaisingExecutionModeAdapters:
    @property
    def execution_mode(self):
        raise TypeError("malformed adapter mode")


class RaisingReplaceString(str):
    def replace(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("adapter mode methods are not trusted")


class RaisingPathLike:
    def __fspath__(self):
        raise RuntimeError("path conversion is not trusted")


class AnimalOnlyReadinessAdapters:
    execution_mode = CONTRACT["executionModes"]["recordedReplay"]

    def workflow_adapters_for_scenario(self, scenario: dict):
        workflow_adapters = DeterministicFixtureAdapters(BASE_FIXTURE)
        original_audit = workflow_adapters.audit_semantics

        def audit_current_scenario(content: dict) -> dict:
            audit = original_audit(content)
            digest = hashlib.sha256(
                json.dumps(
                    content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            audit["contentSha256"] = digest
            audit["observedContentSha256"] = digest
            return audit

        workflow_adapters.audit_semantics = audit_current_scenario
        role = scenario[SCENARIO_FIELDS["role"]]
        workflow_adapters.approved_image_path_override = (
            SHADOW_FIXTURE
            / role.replace("_", "-")
            / "approved-template-image.png"
        )
        return workflow_adapters


class RelabeledRecordedReadinessAdapters(RecordedShadowReadinessAdapters):
    def __init__(self) -> None:
        super().__init__()
        self.execution_mode = CONTRACT["executionModes"]["liveExternal"]

    def release_gate_evidence(self, **kwargs):
        del kwargs
        return {
            field: (
                "0" * 64 if role == "releasePackageDigest" else True
            )
            for role, field in CONTRACT["releaseGateFields"].items()
        }


class VisibleDeviationReadinessAdapters(RecordedShadowReadinessAdapters):
    def template_test_adapters_for_scenario(self, scenario: dict):
        adapter = super().template_test_adapters_for_scenario(scenario)
        original = adapter.inspect_template_test
        review_fields = RULES["templateTestContract"]["reviewFields"]

        def reject_literal_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            result[review_fields["pass"]] = False
            result[review_fields["visibleDeviations"]] = [
                "用户字面输入未在生成图中兑现"
            ]
            return result

        adapter.inspect_template_test = reject_literal_edit
        return adapter


class InvalidWorkflowResultReadinessAdapters(RecordedShadowReadinessAdapters):
    def workflow_adapters_for_scenario(self, scenario: dict):
        del scenario
        return []


class InterruptedReadinessAdapters(RecordedShadowReadinessAdapters):
    def __init__(self, *, interrupt_at: str) -> None:
        super().__init__()
        self.interrupt_at = interrupt_at
        self.installed = False
        self.external_upload_effects = 0

    def workflow_adapters_for_scenario(self, scenario: dict):
        adapter = super().workflow_adapters_for_scenario(scenario)
        role = scenario[SCENARIO_FIELDS["role"]]
        target = CONTRACT["scenarioRoles"]["ordinaryPerson"]
        if role != target or self.installed:
            return adapter
        self.installed = True
        if self.interrupt_at == "poll":
            original_poll = adapter.poll_generation
            interrupted = False

            def poll_once(*args, **kwargs):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise SystemExit("simulated generation process exit")
                return original_poll(*args, **kwargs)

            adapter.poll_generation = poll_once
        else:
            original_upload = adapter.upload
            interrupted = False
            receipt = None

            def upload_once(*args, **kwargs):
                nonlocal interrupted, receipt
                if receipt is None:
                    receipt = original_upload(*args, **kwargs)
                    self.external_upload_effects += 1
                if not interrupted:
                    interrupted = True
                    raise SystemExit("simulated upload process exit")
                return receipt

            adapter.upload = upload_once
        return adapter


class Issue16ShadowReleaseReadinessTest(unittest.TestCase):
    @staticmethod
    def live_scenario_roles() -> set[str]:
        role_by_key = CONTRACT["scenarioRoles"]
        return {
            *(
                role_by_key[key]
                for key in CONTRACT["liveMandatoryScenarioRoleKeys"]
            ),
            role_by_key[CONTRACT["liveSupplementScenarioRoleKeys"][0]],
        }

    def test_review_receipt_separates_comparison_base_from_reviewed_head(self) -> None:
        fields = CONTRACT["reviewReceiptFields"]
        comparison_base = "1" * 40
        reviewed_head = "2" * 40
        runtime_pin_sha = "3" * 64
        receipt = {
            fields["artifactType"]: CONTRACT["reviewReceiptArtifactType"],
            fields["schemaVersion"]: RULES["schemaVersion"],
            fields["axis"]: CONTRACT["reviewAxes"]["standards"],
            fields["comparisonBaseGitCommit"]: comparison_base,
            fields["reviewedGitCommit"]: reviewed_head,
            fields["runtimePinSha256"]: runtime_pin_sha,
            fields["clean"]: True,
            fields["findingCount"]: 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "review-receipt.json"
            path.write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()

            self.assertTrue(
                verify_code_review_receipt(
                    path,
                    expected_sha256=digest,
                    expected_axis=CONTRACT["reviewAxes"]["standards"],
                    expected_comparison_base_git_commit=comparison_base,
                    expected_reviewed_git_commit=reviewed_head,
                    expected_pin_sha256=runtime_pin_sha,
                )
            )
            self.assertFalse(
                verify_code_review_receipt(
                    path,
                    expected_sha256=digest,
                    expected_axis=CONTRACT["reviewAxes"]["standards"],
                    expected_comparison_base_git_commit=comparison_base,
                    expected_reviewed_git_commit=comparison_base,
                    expected_pin_sha256=runtime_pin_sha,
                )
            )
            self.assertFalse(
                verify_code_review_receipt(
                    path,
                    expected_sha256=digest,
                    expected_axis=CONTRACT["reviewAxes"]["standards"],
                    expected_comparison_base_git_commit=reviewed_head,
                    expected_reviewed_git_commit=reviewed_head,
                    expected_pin_sha256=runtime_pin_sha,
                )
            )

    def test_runtime_pin_digest_matches_the_public_production_lineage(self) -> None:
        request = json.loads(
            (BASE_FIXTURE / "request.json").read_text(encoding="utf-8")
        )
        request["sourceImage"] = str(
            (BASE_FIXTURE / request["sourceImage"]).resolve()
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_production(
                request,
                Path(temporary),
                DeterministicFixtureAdapters(BASE_FIXTURE),
            )
            pin_path = result.output_dir / "production-pin.json"

            self.assertEqual("completed", result.outcome)
            self.assertEqual(
                hashlib.sha256(pin_path.read_bytes()).hexdigest(),
                runtime_production_pin_sha256(ROOT),
            )

    def test_forward_replay_uses_the_verified_installed_runtime_corpus(self) -> None:
        forward = recorded_shadow_request()[CONTRACT["requestFields"]["forwardScenario"]]
        production = forward[SCENARIO_FIELDS["productionRequest"]]
        corpus_item = next(
            item
            for item in CORPUS["scenarios"]
            if item["role"] == CONTRACT["forwardScenarioRole"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed_runtime = (root / "installed-runtime").resolve()
            installed_source = (
                installed_runtime
                / "fixtures"
                / "shadow-release"
                / corpus_item["sourcePath"]
            )
            installed_source.parent.mkdir(parents=True)
            shutil.copyfile(production["sourceImage"], installed_source)
            shutil.copyfile(
                SHADOW_FIXTURE / "unseen-forward" / "request.json",
                installed_source.parent / "request.json",
            )
            production["sourceImage"] = str(installed_source)
            result = run_production(
                production,
                root / "forward-output",
                DeterministicFixtureAdapters(SHADOW_FIXTURE / "unseen-forward"),
            )
            lineage = readiness_module._lineage_sha_by_role(
                result.output_dir,
                CONTRACT,
            )

            self.assertEqual("completed", result.outcome)
            self.assertIsNotNone(lineage)
            self.assertTrue(
                readiness_module._request_scenario_matches_corpus(
                    forward,
                    corpus_item,
                    CONTRACT,
                    corpus_runtime_root=installed_runtime,
                )
            )
            self.assertIsNone(
                readiness_module._scenario_evidence_error(
                    scenario=forward,
                    result=result,
                    lineage=lineage,
                    corpus_item=corpus_item,
                    corpus_runtime_root=installed_runtime,
                    rules=RULES,
                    contract=CONTRACT,
                )
            )
            evidence_fields = CONTRACT["releaseGateEvidenceFields"]
            self.assertIsNotNone(
                readiness_module._installed_forward_report(
                    forward,
                    {
                        evidence_fields["installedForwardOutputPath"]: str(
                            result.output_dir
                        ),
                        evidence_fields["installedRuntimePath"]: str(
                            installed_runtime
                        ),
                    },
                    rules=RULES,
                    contract=CONTRACT,
                )
            )

    def test_completion_verifier_reads_requests_from_the_installed_corpus(
        self,
    ) -> None:
        request_fields = CONTRACT["requestFields"]
        evidence_fields = CONTRACT["releaseGateEvidenceFields"]
        ledger_fields = CONTRACT["requestLedgerFields"]
        gate_fields = CONTRACT["releaseGateFields"]
        release_contract = RULES["releaseManagementContract"]
        lock_fields = release_contract["lockFields"]
        release_digest = "1" * 64
        git_commit = "2" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = root / "candidate"
            candidate.mkdir()
            installed_runtime = root / "installed-runtime"
            shutil.copytree(
                SHADOW_FIXTURE,
                installed_runtime / "fixtures" / "shadow-release",
            )
            evidence = {
                evidence_fields["releasePackagePath"]: str(candidate),
                evidence_fields["expectedReleaseDigest"]: release_digest,
                evidence_fields["installedRuntimePath"]: str(installed_runtime),
                evidence_fields["installedForwardOutputPath"]: str(
                    root / "installed-forward"
                ),
                evidence_fields["installedForwardManifestSha256"]: "3" * 64,
                evidence_fields["standardsReviewReceiptPath"]: str(
                    root / "standards-review.json"
                ),
                evidence_fields["standardsReviewReceiptSha256"]: "4" * 64,
                evidence_fields["specReviewReceiptPath"]: str(
                    root / "spec-review.json"
                ),
                evidence_fields["specReviewReceiptSha256"]: "5" * 64,
            }
            request = live_shadow_request(
                evidence,
                supplemental_role=CONTRACT["scenarioRoles"]["genericAnimal"],
            )
            corpus_by_role = {
                item["role"]: item for item in CORPUS["scenarios"]
            }
            for scenario in [
                *request[request_fields["scenarios"]],
                request[request_fields["forwardScenario"]],
            ]:
                role = scenario[SCENARIO_FIELDS["role"]]
                scenario[SCENARIO_FIELDS["productionRequest"]]["sourceImage"] = str(
                    installed_runtime
                    / "fixtures"
                    / "shadow-release"
                    / corpus_by_role[role]["sourcePath"]
                )
            readiness = root / "readiness"
            readiness.mkdir()
            ledger = {
                ledger_fields["artifactType"]: CONTRACT["requestArtifactType"],
                ledger_fields["schemaVersion"]: RULES["schemaVersion"],
                ledger_fields["requestSha256"]: "6" * 64,
                ledger_fields["corpusSha256"]: "7" * 64,
                ledger_fields["request"]: request,
            }
            (readiness / CONTRACT["requestFileName"]).write_text(
                json.dumps(ledger),
                encoding="utf-8",
            )
            for name in (CONTRACT["reportFileName"], CONTRACT["completionFileName"]):
                (readiness / name).write_text("{}", encoding="utf-8")
            (candidate / release_contract["lockFileName"]).write_text(
                json.dumps(
                    {
                        lock_fields["releaseDigest"]: release_digest,
                        lock_fields["gitCommit"]: git_commit,
                    }
                ),
                encoding="utf-8",
            )
            terminal_report = {
                REPORT_FIELDS["pass"]: True,
                REPORT_FIELDS["releaseEligible"]: True,
                REPORT_FIELDS["releaseGates"]: {
                    gate_fields["releasePackageDigest"]: release_digest
                },
            }

            with mock.patch.object(
                readiness_module,
                "_existing_terminal_report",
                return_value=("complete", terminal_report),
            ):
                result = verify_release_readiness_completion(
                    readiness,
                    expected_package_path=candidate,
                    expected_release_digest=release_digest,
                    expected_git_commit=git_commit,
                )

            self.assertTrue(result["pass"])

    def test_promotion_verifier_rejects_symlink_and_runtime_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual-readiness"
            actual.mkdir()
            linked = root / "linked-readiness"
            linked.symlink_to(actual, target_is_directory=True)

            for unsafe in (linked, ROOT):
                result = verify_release_readiness_completion(
                    unsafe,
                    expected_package_path=ROOT,
                    expected_release_digest="0" * 64,
                    expected_git_commit="0" * 40,
                )
                self.assertFalse(result["pass"])
                self.assertEqual(
                    "readiness workspace path is unsafe", result["message"]
                )

    @staticmethod
    def complete_scenarios() -> list[dict]:
        return recorded_shadow_request()[CONTRACT["requestFields"]["scenarios"]]

    def test_live_request_uses_three_mandatory_roles_and_one_supplement(self) -> None:
        scenarios_field = CONTRACT["requestFields"]["scenarios"]
        role_field = SCENARIO_FIELDS["role"]
        role_by_key = CONTRACT["scenarioRoles"]
        mandatory = {
            role_by_key[key]
            for key in CONTRACT["liveMandatoryScenarioRoleKeys"]
        }

        default_roles = {
            item[role_field] for item in live_shadow_request()[scenarios_field]
        }
        animal_roles = {
            item[role_field]
            for item in live_shadow_request(
                supplemental_role=role_by_key["genericAnimal"]
            )[scenarios_field]
        }

        self.assertEqual(
            mandatory | {role_by_key["knownCharacterIp"]}, default_roles
        )
        self.assertEqual(
            mandatory | {role_by_key["genericAnimal"]}, animal_roles
        )
        self.assertEqual(4, len(default_roles))

    def test_live_request_rejects_a_non_supplement_role(self) -> None:
        with self.assertRaises(ValueError):
            live_shadow_request(
                supplemental_role=CONTRACT["scenarioRoles"]["genericObject"]
            )

    def test_live_request_missing_a_mandatory_role_is_rejected_before_side_effects(
        self,
    ) -> None:
        request = live_shadow_request()
        scenarios_field = CONTRACT["requestFields"]["scenarios"]
        ordinary_role = CONTRACT["scenarioRoles"]["ordinaryPerson"]
        request[scenarios_field] = [
            item
            for item in request[scenarios_field]
            if item[SCENARIO_FIELDS["role"]] != ordinary_role
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            report = run_release_readiness(
                request, output_root, RelabeledRecordedReadinessAdapters()
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["coverageMissing"], report[REPORT_FIELDS["errorCode"]]
        )
        self.assertFalse(output_root.exists())

    def test_incomplete_shadow_corpus_is_rejected_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"

            report = run_release_readiness(
                {
                    CONTRACT["requestFields"]["scenarios"]: [],
                    CONTRACT["requestFields"]["forwardScenario"]: None,
                    CONTRACT["requestFields"]["releaseGateEvidence"]: None,
                },
                output_root,
                NeverCalledReadinessAdapters(),
            )

            self.assertFalse(report[REPORT_FIELDS["pass"]])
            self.assertEqual(
                ERROR_CODES["coverageMissing"],
                report[REPORT_FIELDS["errorCode"]],
            )
            self.assertFalse(output_root.exists())

    def test_complete_shadow_corpus_runs_each_item_through_the_public_workflow(self) -> None:
        request = recorded_shadow_request()
        scenarios = request[CONTRACT["requestFields"]["scenarios"]]
        adapters = RecordedShadowReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"

            report = run_release_readiness(
                request,
                output_root,
                adapters,
            )

            self.assertTrue(report[REPORT_FIELDS["pass"]])
            self.assertFalse(report[REPORT_FIELDS["releaseEligible"]])
            report_path = Path(report[REPORT_FIELDS["reportPath"]])
            self.assertTrue(report_path.is_file())
            self.assertTrue((output_root / CONTRACT["completionFileName"]).is_file())
            self.assertEqual(report, json.loads(report_path.read_text(encoding="utf-8")))
            self.assertEqual(
                hashlib.sha256(
                    (SHADOW_FIXTURE / "corpus.json").read_bytes()
                ).hexdigest(),
                report[REPORT_FIELDS["corpusSha256"]],
            )
            scenario_reports = report[REPORT_FIELDS["scenarios"]]
            self.assertEqual(len(scenarios), len(scenario_reports))
            for scenario_report in scenario_reports:
                self.assertTrue(scenario_report[SCENARIO_REPORT_FIELDS["pass"]])
                self.assertEqual(
                    set(CONTRACT["requiredLineageArtifacts"]),
                    set(
                        scenario_report[
                            SCENARIO_REPORT_FIELDS["lineageSha256ByRole"]
                        ]
                    ),
                )
                formal_path = (
                    Path(scenario_report[SCENARIO_REPORT_FIELDS["outputDirectory"]])
                    / "gallery-template.json"
                )
                formal = json.loads(formal_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    set(RULES["formalProjection"]["topLevel"].values()),
                    set(formal),
                )
                self.assertEqual(formal["cover"], formal["referenceImage"])
                self.assertEqual(
                    hashlib.sha256(formal_path.read_bytes()).hexdigest(),
                    scenario_report[
                        SCENARIO_REPORT_FIELDS["formalTemplateSha256"]
                    ],
                )
            self.assertEqual(
                len(scenarios),
                len(
                    {
                        json.loads(
                            (
                                Path(item[SCENARIO_REPORT_FIELDS["outputDirectory"]])
                                / "gallery-template.json"
                            ).read_text(encoding="utf-8")
                        )["key"]
                        for item in scenario_reports
                    }
                ),
            )

            sampled_roles = report[REPORT_FIELDS["templateTestScenarioRoles"]]
            self.assertEqual(CONTRACT["templateTest"]["sampleCount"], len(sampled_roles))
            by_role = {
                item[SCENARIO_REPORT_FIELDS["role"]]: item
                for item in scenario_reports
            }
            for role in sampled_roles:
                item = by_role[role]
                self.assertEqual(
                    "completed",
                    item[SCENARIO_REPORT_FIELDS["templateTestOutcome"]],
                )
                t1_report_path = Path(
                    item[SCENARIO_REPORT_FIELDS["templateTestOutputDirectory"]]
                ) / RULES["templateTestContract"]["artifactNames"]["report"]
                self.assertEqual(
                    hashlib.sha256(t1_report_path.read_bytes()).hexdigest(),
                    item[SCENARIO_REPORT_FIELDS["templateTestReportSha256"]],
                )
                t1_report = json.loads(t1_report_path.read_text(encoding="utf-8"))
                cases = t1_report[RULES["templateTestContract"]["reportFields"]["cases"]]
                self.assertEqual(2, len(cases))
                self.assertTrue(
                    all(
                        case[
                            RULES["templateTestContract"]["caseReportFields"][
                                "resolvedPrompt"
                            ]
                        ]
                        == case[
                            RULES["templateTestContract"]["caseReportFields"][
                                "generationRequest"
                            ]
                        ][
                            RULES["templateTestContract"]["generationRequestFields"][
                                "prompt"
                            ]
                        ]
                        for case in cases
                    )
                )

            side_effect_counts = {
                role: (
                    len(item.submission_calls),
                    len(item.poll_calls),
                    len(item.upload_calls),
                )
                for role, item in adapters.scenario_adapters.items()
            }
            resumed = run_release_readiness(
                request,
                output_root,
                adapters,
            )
            self.assertEqual(report, resumed)
            self.assertEqual(
                side_effect_counts,
                {
                    role: (
                        len(item.submission_calls),
                        len(item.poll_calls),
                        len(item.upload_calls),
                    )
                    for role, item in adapters.scenario_adapters.items()
                },
            )

            forward = report[REPORT_FIELDS["forwardScenario"]]
            self.assertTrue(forward[SCENARIO_REPORT_FIELDS["pass"]])
            self.assertEqual(
                CONTRACT["forwardScenarioRole"],
                forward[SCENARIO_REPORT_FIELDS["role"]],
            )
            self.assertNotIn(
                forward[SCENARIO_REPORT_FIELDS["lineageSha256ByRole"]][
                    "sourceImage"
                ],
                {
                    item[SCENARIO_REPORT_FIELDS["lineageSha256ByRole"]][
                        "sourceImage"
                    ]
                    for item in scenario_reports
                },
            )

    def test_text_dense_template_keeps_secondary_copy_in_the_whole_prompt_only(self) -> None:
        fixture_dir = SHADOW_FIXTURE / "text-dense"
        request = json.loads(
            (fixture_dir / "request.json").read_text(encoding="utf-8")
        )
        request["sourceImage"] = str(fixture_dir / request["sourceImage"])
        secondary_copy = "EXPOSITION\nPeinture—Sculpture"

        with tempfile.TemporaryDirectory() as temporary:
            result = run_production(
                request,
                Path(temporary),
                DeterministicFixtureAdapters(fixture_dir),
            )

            self.assertEqual(RULES["resultStates"]["completed"], result.state)
            formal = json.loads(
                (result.output_dir / "gallery-template.json").read_text(
                    encoding="utf-8"
                )
            )
            editable = json.loads(
                (result.output_dir / "editable-template-spec.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertIn(secondary_copy, formal["promptTemplate"])
        self.assertIn(secondary_copy, editable["freeEditableContent"])
        self.assertEqual(
            {"poster_figure", "headline_text", "poster_tone"},
            {item["id"] for item in formal["inputSchema"]},
        )

    def test_unseen_forward_fixture_runs_without_an_image_path_override(self) -> None:
        request = recorded_shadow_request()[
            CONTRACT["requestFields"]["forwardScenario"]
        ][SCENARIO_FIELDS["productionRequest"]]
        adapters = DeterministicFixtureAdapters(SHADOW_FIXTURE / "unseen-forward")
        with tempfile.TemporaryDirectory() as temporary:
            result = run_production(request, Path(temporary), adapters)

        self.assertEqual("completed", result.outcome)
        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(1, len(adapters.upload_calls))

    def test_live_release_preflight_names_every_missing_external_credential(self) -> None:
        evidence = live_release_readiness_preflight({})
        fields = CONTRACT["externalExecutionFields"]
        self.assertEqual(
            CONTRACT["externalExecutionStatuses"]["notRunMissingCredentials"],
            evidence[fields["status"]],
        )
        self.assertEqual(
            set(CONTRACT["liveCredentialEnvironment"]),
            set(evidence[fields["missingCredentialRoles"]]),
        )

    def test_live_adapter_requires_an_independent_reviewer_for_every_role(self) -> None:
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        deterministic_reviewers = {
            role: DeterministicFixtureAdapters(BASE_FIXTURE)
            for role in self.live_scenario_roles()
        }
        with mock.patch.dict(os.environ, credentials, clear=False):
            with self.assertRaises(RuntimeError):
                LiveShadowReadinessAdapters(
                    live_review_adapters_by_role=deterministic_reviewers
                )

    def test_live_adapter_does_not_require_a_second_forward_reviewer(self) -> None:
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        reviewers = {
            role: NeverCalledLiveReviewer()
            for role in self.live_scenario_roles()
        }
        with mock.patch.dict(os.environ, credentials, clear=False):
            adapters = LiveShadowReadinessAdapters(
                live_review_adapters_by_role=reviewers
            )

        self.assertEqual({}, adapters.scenario_adapters)

    def test_live_supplement_mismatch_stops_before_external_side_effects(self) -> None:
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        reviewers = {
            role: NeverCalledLiveReviewer()
            for role in self.live_scenario_roles()
        }
        request = live_shadow_request(
            supplemental_role=CONTRACT["scenarioRoles"]["genericAnimal"]
        )
        with mock.patch.dict(os.environ, credentials, clear=False):
            adapters = LiveShadowReadinessAdapters(
                live_review_adapters_by_role=reviewers
            )
            with tempfile.TemporaryDirectory() as temporary:
                output_root = Path(temporary) / "readiness"
                report = run_release_readiness(request, output_root, adapters)

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["liveEvidenceMismatch"],
            report[REPORT_FIELDS["errorCode"]],
        )
        self.assertEqual({}, adapters.scenario_adapters)
        self.assertFalse(output_root.exists())

    def test_live_request_requires_installed_forward_evidence_before_side_effects(
        self,
    ) -> None:
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        reviewers = {
            role: NeverCalledLiveReviewer()
            for role in self.live_scenario_roles()
        }
        with mock.patch.dict(os.environ, credentials, clear=False):
            adapters = LiveShadowReadinessAdapters(
                live_review_adapters_by_role=reviewers
            )
            with tempfile.TemporaryDirectory() as temporary:
                output_root = Path(temporary) / "readiness"
                report = run_release_readiness(
                    live_shadow_request(), output_root, adapters
                )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["releaseGateIncomplete"],
            report[REPORT_FIELDS["errorCode"]],
        )
        self.assertEqual({}, adapters.scenario_adapters)
        self.assertFalse(output_root.exists())

    def test_recorded_adapter_cannot_claim_live_external_execution(self) -> None:
        request = recorded_shadow_request()
        for scenario in [
            *request[CONTRACT["requestFields"]["scenarios"]],
            request[CONTRACT["requestFields"]["forwardScenario"]],
        ]:
            scenario[SCENARIO_FIELDS["executionMode"]] = CONTRACT[
                "executionModes"
            ]["liveExternal"]
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            report = run_release_readiness(
                request, output_root, RecordedShadowReadinessAdapters()
            )
        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]])
        self.assertFalse(output_root.exists())

    def test_relabeling_recorded_adapters_cannot_satisfy_live_evidence(self) -> None:
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        with mock.patch.dict(os.environ, credentials, clear=False):
            with tempfile.TemporaryDirectory() as temporary:
                report = run_release_readiness(
                    live_shadow_request(),
                    Path(temporary) / "readiness",
                    RelabeledRecordedReadinessAdapters(),
                )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertFalse(report[REPORT_FIELDS["releaseEligible"]])
        self.assertEqual(
            ERROR_CODES["liveEvidenceMismatch"],
            report[REPORT_FIELDS["errorCode"]],
        )

    def test_unconstructed_live_adapter_cannot_inject_scenario_factories(self) -> None:
        forged = object.__new__(LiveShadowReadinessAdapters)
        forged.execution_mode = CONTRACT["executionModes"]["liveExternal"]
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        with mock.patch.dict(os.environ, credentials, clear=False):
            with tempfile.TemporaryDirectory() as temporary:
                output_root = Path(temporary) / "readiness"
                report = run_release_readiness(
                    live_shadow_request(), output_root, forged
                )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertFalse(report[REPORT_FIELDS["releaseEligible"]])
        self.assertEqual(
            ERROR_CODES["liveEvidenceMismatch"],
            report[REPORT_FIELDS["errorCode"]],
        )
        self.assertFalse(output_root.exists())

    def test_invalid_release_gate_bundle_stops_before_live_side_effects(self) -> None:
        evidence = {
            field: (
                "0" * 64
                if role == "expectedReleaseDigest"
                else "/missing/release-evidence"
            )
            for role, field in CONTRACT["releaseGateEvidenceFields"].items()
        }
        credentials = {
            variable: f"dummy-{role}"
            for role, variable in CONTRACT["liveCredentialEnvironment"].items()
        }
        reviewers = {
            role: NeverCalledLiveReviewer()
            for role in self.live_scenario_roles()
        }
        with mock.patch.dict(os.environ, credentials, clear=False):
            adapters = LiveShadowReadinessAdapters(
                live_review_adapters_by_role=reviewers
            )
            with tempfile.TemporaryDirectory() as temporary:
                output_root = Path(temporary) / "readiness"
                report = run_release_readiness(
                    live_shadow_request(evidence), output_root, adapters
                )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertFalse(report[REPORT_FIELDS["releaseEligible"]])
        self.assertEqual(
            ERROR_CODES["releaseGateIncomplete"],
            report[REPORT_FIELDS["errorCode"]],
        )
        self.assertEqual({}, adapters.scenario_adapters)
        self.assertFalse(output_root.exists())

    def test_t1_visible_deviation_blocks_release_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = run_release_readiness(
                recorded_shadow_request(),
                Path(temporary) / "readiness",
                VisibleDeviationReadinessAdapters(),
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["templateTestFailure"],
            report[REPORT_FIELDS["errorCode"]],
        )

    def test_changed_request_is_rejected_before_new_external_side_effects(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        request = recorded_shadow_request()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            first = run_release_readiness(request, output_root, adapters)
            self.assertTrue(first[REPORT_FIELDS["pass"]])
            calls_before = {
                role: (len(adapter.submission_calls), len(adapter.upload_calls))
                for role, adapter in adapters.scenario_adapters.items()
            }
            changed = recorded_shadow_request()
            production = changed[CONTRACT["requestFields"]["scenarios"]][0][
                SCENARIO_FIELDS["productionRequest"]
            ]
            production["productionItemId"] = "shadow-ordinary-person-changed"
            production["templateKey"] = "shadow-ordinary-person-changed"

            second = run_release_readiness(changed, output_root, adapters)

            self.assertFalse(second[REPORT_FIELDS["pass"]])
            self.assertFalse(second[REPORT_FIELDS["releaseEligible"]])
            self.assertEqual(
                calls_before,
                {
                    role: (len(adapter.submission_calls), len(adapter.upload_calls))
                    for role, adapter in adapters.scenario_adapters.items()
                },
            )
            self.assertFalse(
                (output_root / "production-items" / "shadow-ordinary-person-changed").exists()
            )

    def test_conflicting_report_is_rejected_before_any_external_side_effect(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            output_root.mkdir()
            report_path = output_root / CONTRACT["reportFileName"]
            report_path.write_text("{}\n", encoding="utf-8")

            report = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

            self.assertFalse(report[REPORT_FIELDS["pass"]])
            self.assertFalse(report[REPORT_FIELDS["releaseEligible"]])
            self.assertEqual(
                ERROR_CODES["reportConflict"], report[REPORT_FIELDS["errorCode"]]
            )
            self.assertEqual({}, adapters.scenario_adapters)
            self.assertEqual([report_path], list(output_root.iterdir()))

    def test_completed_report_and_artifacts_are_replayed_before_adapter_calls(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            first = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            calls_before = {
                role: (len(adapter.submission_calls), len(adapter.upload_calls))
                for role, adapter in adapters.scenario_adapters.items()
            }
            report_path = Path(first[REPORT_FIELDS["reportPath"]])
            original_report = report_path.read_bytes()
            forged_report = json.loads(original_report)
            forged_report[REPORT_FIELDS["scenarios"]][0][
                SCENARIO_REPORT_FIELDS["pass"]
            ] = False
            report_path.write_text(
                json.dumps(forged_report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            forged = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            self.assertEqual(
                ERROR_CODES["reportConflict"], forged[REPORT_FIELDS["errorCode"]]
            )
            self.assertEqual(
                calls_before,
                {
                    role: (len(adapter.submission_calls), len(adapter.upload_calls))
                    for role, adapter in adapters.scenario_adapters.items()
                },
            )

            report_path.write_bytes(original_report)
            first_output = Path(
                first[REPORT_FIELDS["scenarios"]][0][
                    SCENARIO_REPORT_FIELDS["outputDirectory"]
                ]
            )
            (first_output / "production-manifest.json").unlink()
            missing = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

            self.assertEqual(
                ERROR_CODES["reportConflict"], missing[REPORT_FIELDS["errorCode"]]
            )
            self.assertEqual(
                calls_before,
                {
                    role: (len(adapter.submission_calls), len(adapter.upload_calls))
                    for role, adapter in adapters.scenario_adapters.items()
                },
            )

    def test_completion_without_its_report_cannot_start_external_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source-readiness"
            first = run_release_readiness(
                recorded_shadow_request(),
                source_root,
                RecordedShadowReadinessAdapters(),
            )
            self.assertTrue(first[REPORT_FIELDS["pass"]])
            target_root = root / "target-readiness"
            target_root.mkdir()
            for role in ("requestFileName", "completionFileName"):
                name = CONTRACT[role]
                (target_root / name).write_bytes((source_root / name).read_bytes())
            adapters = RecordedShadowReadinessAdapters()

            report = run_release_readiness(
                recorded_shadow_request(), target_root, adapters
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["reportConflict"], report[REPORT_FIELDS["errorCode"]]
        )
        self.assertEqual({}, adapters.scenario_adapters)

    def test_recorded_completion_cannot_claim_live_release_eligibility(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            first = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            calls_before = {
                role: (len(adapter.submission_calls), len(adapter.upload_calls))
                for role, adapter in adapters.scenario_adapters.items()
            }
            report_path = Path(first[REPORT_FIELDS["reportPath"]])
            forged = json.loads(report_path.read_text(encoding="utf-8"))
            external_fields = CONTRACT["externalExecutionFields"]
            forged[REPORT_FIELDS["externalExecution"]] = {
                external_fields["status"]: CONTRACT["externalExecutionStatuses"][
                    "completed"
                ],
                external_fields["missingCredentialRoles"]: [],
            }
            forged[REPORT_FIELDS["releaseGates"]] = {
                field: (
                    "0" * 64
                    if role in {"releasePackageDigest", "runtimePinSha256"}
                    else True
                )
                for role, field in CONTRACT["releaseGateFields"].items()
            }
            forged[REPORT_FIELDS["releaseEligible"]] = True
            report_path.write_text(
                json.dumps(forged, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            completion_path = output_root / CONTRACT["completionFileName"]
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            completion[
                CONTRACT["completionFields"]["reportSha256"]
            ] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            completion_path.write_text(
                json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            replay = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

        self.assertFalse(replay[REPORT_FIELDS["pass"]])
        self.assertFalse(replay[REPORT_FIELDS["releaseEligible"]])
        self.assertEqual(
            ERROR_CODES["reportConflict"], replay[REPORT_FIELDS["errorCode"]]
        )
        self.assertEqual(
            calls_before,
            {
                role: (len(adapter.submission_calls), len(adapter.upload_calls))
                for role, adapter in adapters.scenario_adapters.items()
            },
        )

    def test_failed_completion_rejects_malformed_nested_report_shape(self) -> None:
        adapters = VisibleDeviationReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            first = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            self.assertFalse(first[REPORT_FIELDS["pass"]])
            replay = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            self.assertEqual(first, replay)
            report_path = Path(first[REPORT_FIELDS["reportPath"]])
            malformed = json.loads(report_path.read_text(encoding="utf-8"))
            malformed[REPORT_FIELDS["scenarios"]][0] = {}
            report_path.write_text(
                json.dumps(malformed, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            completion_path = output_root / CONTRACT["completionFileName"]
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            completion[
                CONTRACT["completionFields"]["reportSha256"]
            ] = hashlib.sha256(report_path.read_bytes()).hexdigest()
            completion_path.write_text(
                json.dumps(completion, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )

            rejected = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

        self.assertFalse(rejected[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["reportConflict"], rejected[REPORT_FIELDS["errorCode"]]
        )

    def test_production_failure_report_is_idempotently_replayed(self) -> None:
        adapters = InvalidWorkflowResultReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            first = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            replay = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

        self.assertFalse(first[REPORT_FIELDS["pass"]])
        self.assertEqual(first, replay)
        self.assertEqual(
            ERROR_CODES["productionFailure"],
            first[REPORT_FIELDS["scenarios"]][0][
                SCENARIO_REPORT_FIELDS["errorCode"]
            ],
        )

    def test_adapter_mode_exception_returns_a_stable_invalid_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            report = run_release_readiness(
                recorded_shadow_request(), output_root, RaisingExecutionModeAdapters()
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]])
        self.assertFalse(output_root.exists())

    def test_adapter_mode_must_be_an_exact_machine_string(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        adapters.execution_mode = RaisingReplaceString(
            CONTRACT["executionModes"]["recordedReplay"]
        )
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            report = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(
            ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]]
        )
        self.assertEqual({}, adapters.scenario_adapters)
        self.assertFalse(output_root.exists())

    def test_malformed_output_root_returns_a_stable_invalid_request(self) -> None:
        for output_root in (None, [], {}, b"readiness", RaisingPathLike()):
            with self.subTest(output_root=output_root):
                adapters = RecordedShadowReadinessAdapters()
                report = run_release_readiness(
                    recorded_shadow_request(), output_root, adapters
                )
                self.assertFalse(report[REPORT_FIELDS["pass"]])
                self.assertEqual(
                    ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]]
                )
                self.assertEqual({}, adapters.scenario_adapters)

    def test_non_string_role_returns_a_stable_invalid_request(self) -> None:
        request = recorded_shadow_request()
        request[CONTRACT["requestFields"]["scenarios"]][0][
            SCENARIO_FIELDS["role"]
        ] = {}
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            report = run_release_readiness(
                request, output_root, NeverCalledReadinessAdapters()
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]])
        self.assertFalse(output_root.exists())

    def test_malformed_nested_scenario_is_rejected_before_any_adapter_call(self) -> None:
        request = recorded_shadow_request()
        request[CONTRACT["requestFields"]["scenarios"]][0][
            SCENARIO_FIELDS["sourceProvenance"]
        ] = []
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            report = run_release_readiness(
                request, output_root, NeverCalledReadinessAdapters()
            )
            self.assertFalse(report[REPORT_FIELDS["pass"]])
            self.assertEqual(
                ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]]
            )
            self.assertFalse(output_root.exists())

    def test_output_path_cannot_cross_an_intermediate_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real" / "nested"
            real.mkdir(parents=True)
            alias = root / "alias"
            alias.symlink_to(root / "real", target_is_directory=True)
            output_root = alias / "nested" / "readiness"
            report = run_release_readiness(
                recorded_shadow_request(),
                output_root,
                RecordedShadowReadinessAdapters(),
            )
            self.assertFalse(report[REPORT_FIELDS["pass"]])
            self.assertEqual(
                ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]]
            )
            self.assertFalse((real / "readiness").exists())

    def test_fixed_workspace_cannot_be_redirected_through_a_symlink(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_root = root / "readiness"
            external = root / "external"
            output_root.mkdir()
            external.mkdir()
            (output_root / CONTRACT["workspaceDirectories"]["production"]).symlink_to(
                external, target_is_directory=True
            )

            report = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )
            external_contents = list(external.iterdir())

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        self.assertEqual(ERROR_CODES["invalidRequest"], report[REPORT_FIELDS["errorCode"]])
        self.assertEqual({}, adapters.scenario_adapters)
        self.assertEqual([], external_contents)

    def test_scenario_order_is_normalized_before_execution_and_reporting(self) -> None:
        adapters = RecordedShadowReadinessAdapters()
        request = recorded_shadow_request()
        scenarios_field = CONTRACT["requestFields"]["scenarios"]
        request[scenarios_field].reverse()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            first = run_release_readiness(request, output_root, adapters)
            calls_before = {
                role: (len(adapter.submission_calls), len(adapter.upload_calls))
                for role, adapter in adapters.scenario_adapters.items()
            }
            second = run_release_readiness(
                recorded_shadow_request(), output_root, adapters
            )

        self.assertTrue(first[REPORT_FIELDS["pass"]])
        self.assertEqual(first, second)
        self.assertEqual(
            list(CONTRACT["scenarioRoles"].values()),
            [
                item[SCENARIO_REPORT_FIELDS["role"]]
                for item in first[REPORT_FIELDS["scenarios"]]
            ],
        )
        self.assertEqual(
            calls_before,
            {
                role: (len(adapter.submission_calls), len(adapter.upload_calls))
                for role, adapter in adapters.scenario_adapters.items()
            },
        )

    def test_generation_process_exit_resumes_without_duplicate_submission(self) -> None:
        adapters = InterruptedReadinessAdapters(interrupt_at="poll")
        request = recorded_shadow_request()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            with self.assertRaises(SystemExit):
                run_release_readiness(request, output_root, adapters)
            ordinary = adapters.scenario_adapters[
                CONTRACT["scenarioRoles"]["ordinaryPerson"]
            ]
            self.assertEqual(1, len(ordinary.submission_calls))
            interrupted_task_id = ordinary.submission_calls[0]["taskId"]

            report = run_release_readiness(request, output_root, adapters)

            self.assertTrue(report[REPORT_FIELDS["pass"]])
            self.assertEqual(
                1,
                sum(
                    item["taskId"] == interrupted_task_id
                    for item in ordinary.submission_calls
                ),
            )
            self.assertEqual(
                len(ordinary.submission_calls),
                len({item["taskId"] for item in ordinary.submission_calls}),
            )
            self.assertEqual(3, len(ordinary.poll_calls))
            self.assertEqual(1, len(ordinary.upload_calls))

    def test_upload_process_exit_resumes_without_duplicate_external_put(self) -> None:
        adapters = InterruptedReadinessAdapters(interrupt_at="upload")
        request = recorded_shadow_request()
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "readiness"
            with self.assertRaises(SystemExit):
                run_release_readiness(request, output_root, adapters)
            self.assertEqual(1, adapters.external_upload_effects)

            report = run_release_readiness(request, output_root, adapters)

            self.assertTrue(report[REPORT_FIELDS["pass"]])
            self.assertEqual(1, adapters.external_upload_effects)

    def test_scenario_roles_cannot_be_satisfied_by_an_unrelated_animal_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = run_release_readiness(
                recorded_shadow_request(),
                Path(temporary) / "readiness",
                AnimalOnlyReadinessAdapters(),
            )

        self.assertFalse(report[REPORT_FIELDS["pass"]])
        failures = {
            item[SCENARIO_REPORT_FIELDS["role"]]: item[
                SCENARIO_REPORT_FIELDS["errorCode"]
            ]
            for item in report[REPORT_FIELDS["scenarios"]]
            if not item[SCENARIO_REPORT_FIELDS["pass"]]
        }
        self.assertEqual(
            ERROR_CODES["scenarioRoleMismatch"],
            failures[CONTRACT["scenarioRoles"]["ordinaryPerson"]],
        )
        self.assertEqual(
            ERROR_CODES["scenarioRoleMismatch"],
            failures[CONTRACT["scenarioRoles"]["complexMultiInstance"]],
        )


if __name__ == "__main__":
    unittest.main()
