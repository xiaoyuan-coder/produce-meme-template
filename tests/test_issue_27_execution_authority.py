from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from scripts.export_gallery_templates import ExportError, export_gallery_templates
from scripts.produce_meme_template import (
    DeterministicFixtureAdapters,
    build_live_production_adapters,
    run_production,
)
from tests.live_production_support import build_live_test_adapters


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
FIXED_TIME = datetime.fromisoformat("2026-08-21T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class BatchSelfCertifyingAdapters(DeterministicFixtureAdapters):
    """Models the 1376–1382 script: fixture generation plus copied reviews."""

    live_review_method_id = "codex-local-visual-inspection"
    live_authoring_analysis_method_id = "batch-copied-authoring-analysis"
    live_authoring_audit_method_id = "batch-copied-authoring-analysis"


class RoleAdapter:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class UnusedBucket:
    def __getattr__(self, name):
        raise AssertionError(f"stage one cannot access OSS: {name}")


class ProductionExecutionAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "production"
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(
            (FIXTURE / self.request["sourceImage"]).resolve()
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_live_production_rejects_the_batch_fixture_topology_before_p0(self) -> None:
        adapters = BatchSelfCertifyingAdapters(FIXTURE)

        result = run_production(
            {**self.request, "productionItemId": "material-1376-live"},
            self.output_root,
            adapters,
            execution_mode=RULES["productionExecutionContract"]["executionModes"]["liveExternal"],
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )
        self.assertFalse(result.output_dir.exists())
        self.assertEqual([], adapters.submission_calls)
        self.assertEqual([], adapters.poll_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_recorded_replay_is_labeled_and_cannot_be_exported(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "material-1377-replay"},
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            execution_mode=RULES["productionExecutionContract"]["executionModes"]["recordedReplay"],
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", result.outcome)
        profile = load_json(result.output_dir / "production-execution-profile.json")
        self.assertEqual(
            RULES["productionExecutionContract"]["executionModes"]["recordedReplay"],
            profile["executionMode"],
        )
        self.assertIs(False, profile["deliveryEligible"])
        manifest = load_json(result.output_dir / "production-manifest.json")
        self.assertEqual(
            RULES["productionExecutionContract"]["executionModes"]["recordedReplay"],
            manifest["executionMode"],
        )

        with self.assertRaisesRegex(ExportError, "不可交付"):
            export_gallery_templates(
                result.output_dir / "gallery-template.json",
                Path(self.temporary.name) / "records",
                manifest_path=Path(self.temporary.name) / "delivery.json",
            )

    def test_live_readiness_uses_external_providers_without_delivery_authority(
        self,
    ) -> None:
        adapters, fal_client, bucket = build_live_test_adapters(FIXTURE)
        result = run_production(
            {**self.request, "productionItemId": "live-readiness-evidence"},
            self.output_root,
            adapters,
            execution_mode=RULES["productionExecutionContract"][
                "liveReadinessExecutionMode"
            ],
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", result.outcome)
        self.assertEqual(1, len(fal_client.submit_calls))
        self.assertEqual(1, len(bucket.put_calls))
        profile = load_json(result.output_dir / "production-execution-profile.json")
        fields = RULES["productionExecutionContract"]["profileFields"]
        self.assertEqual(
            RULES["productionExecutionContract"]["liveReadinessExecutionMode"],
            profile[fields["executionMode"]],
        )
        self.assertIs(False, profile[fields["deliveryEligible"]])
        with self.assertRaisesRegex(ExportError, "不可交付"):
            export_gallery_templates(
                result.output_dir / "gallery-template.json",
                Path(self.temporary.name) / "readiness-records",
                manifest_path=Path(self.temporary.name) / "readiness-delivery.json",
            )

    def test_live_factory_rejects_self_certified_authoring_audit(self) -> None:
        delegate = DeterministicFixtureAdapters(FIXTURE)
        source = RoleAdapter(delegate)
        source.live_template_identity_method_id = "template-registry-semantic-review"
        visual = RoleAdapter(delegate)
        self_certifying_author = RoleAdapter(delegate)
        semantic = RoleAdapter(delegate)
        visual_contract = RoleAdapter(delegate)
        visual.live_review_method_id = RULES["productionExecutionContract"][
            "liveReviewMethodIds"
        ][0]
        self_certifying_author.live_authoring_analysis_method_id = "same-author"
        self_certifying_author.live_authoring_audit_method_id = "same-author"

        with self.assertRaisesRegex(ValueError, "transitively independent"):
            build_live_production_adapters(
                source_adapter=source,
                visual_review_adapter=visual,
                authoring_analysis_adapter=self_certifying_author,
                authoring_audit_adapter=self_certifying_author,
                semantic_audit_adapter=semantic,
                visual_contract_audit_adapter=visual_contract,
                oss_options={},
            )

    def test_live_factory_rejects_distinct_proxies_over_one_fixture(self) -> None:
        delegate = DeterministicFixtureAdapters(FIXTURE)
        source = RoleAdapter(delegate)
        source.live_template_identity_method_id = "template-registry-semantic-review"
        visual = RoleAdapter(delegate)
        authoring_analysis = RoleAdapter(delegate)
        authoring_audit = RoleAdapter(delegate)
        semantic = RoleAdapter(delegate)
        visual_contract = RoleAdapter(delegate)
        visual.live_review_method_id = RULES["productionExecutionContract"][
            "liveReviewMethodIds"
        ][0]
        authoring_analysis.live_authoring_analysis_method_id = (
            "live-authoring-analysis"
        )
        authoring_audit.live_authoring_audit_method_id = (
            "independent-authoring-audit"
        )

        with self.assertRaisesRegex(ValueError, "transitively independent"):
            build_live_production_adapters(
                source_adapter=source,
                visual_review_adapter=visual,
                authoring_analysis_adapter=authoring_analysis,
                authoring_audit_adapter=authoring_audit,
                semantic_audit_adapter=semantic,
                visual_contract_audit_adapter=visual_contract,
                oss_options={},
            )

    def test_live_stage_one_requires_and_records_installed_runtime_authority(self) -> None:
        adapters, fal_client, bucket = build_live_test_adapters(FIXTURE)
        diagnostic_fields = RULES["releaseManagementContract"]["diagnosticFields"]
        diagnosis = {
            "pass": True,
            diagnostic_fields["installSource"]: "/verified-install/4.0.0",
            diagnostic_fields["errorCodes"]: [],
        }

        with mock.patch(
            "scripts.produce_meme_template.production_runtime.doctor",
            return_value=diagnosis,
        ):
            result = run_production(
                {**self.request, "productionItemId": "installed-live-stage-one"},
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                stage=1,
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual("completed", result.outcome)
        profile = load_json(result.output_dir / "production-execution-profile.json")
        self.assertIs(True, profile["deliveryEligible"])
        self.assertEqual("/verified-install/4.0.0", profile["runtimeInstallSource"])
        self.assertEqual([], fal_client.submit_calls)
        self.assertEqual([], bucket.put_calls)

    def test_live_stage_one_rejects_source_worktree_before_p0(self) -> None:
        adapters, fal_client, bucket = build_live_test_adapters(FIXTURE)

        result = run_production(
            {**self.request, "productionItemId": "source-worktree-live"},
            self.output_root,
            adapters,
            execution_mode=RULES["productionExecutionContract"][
                "executionModes"
            ]["liveExternal"],
            stage=1,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )
        self.assertFalse(result.output_dir.exists())
        self.assertEqual([], fal_client.submit_calls)
        self.assertEqual([], bucket.put_calls)

    def test_live_stage_one_rejects_blank_install_source_before_p0(self) -> None:
        adapters, fal_client, bucket = build_live_test_adapters(FIXTURE)
        diagnostic_fields = RULES["releaseManagementContract"]["diagnosticFields"]
        diagnosis = {
            "pass": True,
            diagnostic_fields["installSource"]: "   ",
            diagnostic_fields["errorCodes"]: [],
        }

        with mock.patch(
            "scripts.produce_meme_template.production_runtime.doctor",
            return_value=diagnosis,
        ):
            result = run_production(
                {**self.request, "productionItemId": "blank-install-source-live"},
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                stage=1,
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )
        self.assertFalse(result.output_dir.exists())
        self.assertEqual([], fal_client.submit_calls)
        self.assertEqual([], bucket.put_calls)

    def test_live_shared_batch_checks_runtime_before_source_analysis(self) -> None:
        class SourceRole:
            live_template_identity_method_id = "template-registry-semantic-review"

            def __init__(self) -> None:
                self.analysis_calls = 0

            def resolve_template_identity(self, *_args):
                raise AssertionError("identity lookup must follow runtime authority")

            def analyze_source(self, *_args):
                self.analysis_calls += 1
                raise AssertionError("source analysis must follow runtime authority")

        class VisualRole:
            live_review_method_id = RULES["productionExecutionContract"][
                "liveReviewMethodIds"
            ][0]

            def inspect_generated(self, *_args):
                raise AssertionError("visual review is outside stage one")

        class AuthoringRole:
            live_authoring_analysis_method_id = "live-authoring-analysis"

            def analyze_approved_with_handoff(self, *_args):
                raise AssertionError("authoring analysis is outside stage one")

        class AuthoringAuditRole:
            live_authoring_audit_method_id = "independent-authoring-audit"

            def audit_authoring_contract(self, *_args):
                raise AssertionError("authoring audit is outside stage one")

        class SemanticRole:
            def audit_semantics(self, *_args):
                raise AssertionError("semantic audit is outside stage one")

        class VisualContractRole:
            def audit_visual_contract(self, *_args):
                raise AssertionError("visual contract audit is outside stage one")

        source = SourceRole()
        adapters = build_live_production_adapters(
            source_adapter=source,
            visual_review_adapter=VisualRole(),
            authoring_analysis_adapter=AuthoringRole(),
            authoring_audit_adapter=AuthoringAuditRole(),
            semantic_audit_adapter=SemanticRole(),
            visual_contract_audit_adapter=VisualContractRole(),
            oss_options={
                "public_base_url": "https://cdn.example.com",
                "bucket": UnusedBucket(),
                "resolve_host": lambda *_args, **_kwargs: [
                    (None, None, None, None, ("93.184.216.34", 443))
                ],
            },
        )
        batch = RULES["batchProductionContract"]
        request_fields = batch["requestFields"]
        policy_fields = batch["sharedPolicyFields"]
        pool_fields = batch["replacementPoolEntryFields"]
        item = {
            **self.request,
            "productionItemId": "live-shared-runtime-item",
        }
        request = {
            request_fields["batchIdentity"]: "live-shared-runtime-batch",
            request_fields["items"]: [item],
            request_fields["sharedPolicy"]: {
                policy_fields["policyIdentity"]: "live-shared-policy",
                policy_fields["policyVersion"]: "1",
                policy_fields["policyRevision"]: "r1",
                policy_fields["scope"]: [item["productionItemId"]],
                policy_fields["replacementPool"]: [
                    {
                        pool_fields["replacementValue"]: "水豚",
                        pool_fields["replacementCategory"]: RULES[
                            "sourceCategories"
                        ]["genericAnimal"],
                    }
                ],
            },
        }

        diagnostic_fields = RULES["releaseManagementContract"][
            "diagnosticFields"
        ]
        with mock.patch(
            "scripts.produce_meme_template.workflow.doctor",
            return_value={
                "pass": True,
                diagnostic_fields["installSource"]: "source-worktree",
                diagnostic_fields["errorCodes"]: [],
            },
        ):
            result = run_production(
                request,
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                stage=1,
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )
        self.assertEqual(0, source.analysis_calls)
        self.assertFalse(self.output_root.exists())

    def test_unchanged_replay_reuses_all_external_side_effects(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {**self.request, "productionItemId": "material-1378-replay"}

        first = run_production(
            request,
            self.output_root,
            adapters,
            execution_mode=RULES["productionExecutionContract"]["executionModes"]["recordedReplay"],
            clock=lambda: FIXED_TIME,
        )
        call_counts = (
            len(adapters.submission_calls),
            len(adapters.poll_calls),
            len(adapters.upload_calls),
        )
        second = run_production(
            request,
            self.output_root,
            adapters,
            execution_mode=RULES["productionExecutionContract"]["executionModes"]["recordedReplay"],
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", first.outcome)
        self.assertEqual("completed", second.outcome)
        self.assertTrue(second.resumed)
        self.assertEqual(
            call_counts,
            (
                len(adapters.submission_calls),
                len(adapters.poll_calls),
                len(adapters.upload_calls),
            ),
        )

    def test_execution_mode_cannot_change_when_resuming_an_item(self) -> None:
        request = {**self.request, "productionItemId": "material-1382-mode-drift"}
        replay = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            execution_mode=RULES["productionExecutionContract"]["executionModes"]["recordedReplay"],
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual("completed", replay.outcome)

        live_attempt = run_production(
            request,
            self.output_root,
            BatchSelfCertifyingAdapters(FIXTURE),
            execution_mode=RULES["productionExecutionContract"]["executionModes"]["liveExternal"],
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", live_attempt.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            live_attempt.error_code,
        )

    def test_live_full_production_replays_lineage_and_reuses_external_calls(
        self,
    ) -> None:
        adapters, fal_client, bucket = build_live_test_adapters(FIXTURE)
        diagnostic_fields = RULES["releaseManagementContract"]["diagnosticFields"]
        diagnosis = {
            "pass": True,
            diagnostic_fields["installSource"]: "/verified-install/4.0.0",
            diagnostic_fields["errorCodes"]: [],
        }
        request = {
            **self.request,
            "productionItemId": "live-full-lineage",
        }

        with mock.patch(
            "scripts.produce_meme_template.production_runtime.doctor",
            return_value=diagnosis,
        ):
            first = run_production(
                request,
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
            )
            second = run_production(
                request,
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual("completed", first.outcome)
        self.assertEqual("completed", second.outcome)
        self.assertTrue(second.resumed)
        self.assertEqual(1, len(fal_client.submit_calls))
        self.assertEqual(1, len(fal_client.status_calls))
        self.assertEqual(1, len(fal_client.result_calls))
        self.assertEqual(1, len(bucket.put_calls))
        delivery = export_gallery_templates(
            first.output_dir / "gallery-template.json",
            Path(self.temporary.name) / "live-records",
            manifest_path=Path(self.temporary.name) / "live-delivery.json",
        )
        self.assertEqual(1, delivery["recordCount"])

    def test_live_p2_rejects_generation_provider_outside_profile(self) -> None:
        adapters, _fal_client, bucket = build_live_test_adapters(FIXTURE)
        original_submit = adapters.submit_generation
        provider_field = RULES["generationExecutionContract"]["submissionFields"][
            "provider"
        ]

        def submit_with_wrong_provider(*args):
            submission = original_submit(*args)
            submission[provider_field] = RULES["generationExecutionContract"][
                "providerRoles"
            ]["deterministicFixture"]
            return submission

        adapters.submit_generation = submit_with_wrong_provider
        diagnostic_fields = RULES["releaseManagementContract"]["diagnosticFields"]
        with mock.patch(
            "scripts.produce_meme_template.production_runtime.doctor",
            return_value={
                "pass": True,
                diagnostic_fields["installSource"]: "/verified-install/4.0.0",
                diagnostic_fields["errorCodes"]: [],
            },
        ):
            result = run_production(
                {**self.request, "productionItemId": "live-wrong-generation-provider"},
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
            )
            resumed = run_production(
                {**self.request, "productionItemId": "live-wrong-generation-provider"},
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )
        self.assertEqual(1, len(_fal_client.submit_calls))
        self.assertEqual([], bucket.put_calls)

    def test_live_p2_rejects_review_method_outside_profile(self) -> None:
        adapters, _fal_client, bucket = build_live_test_adapters(
            FIXTURE,
            emitted_review_method_id="different-live-reviewer",
        )
        diagnostic_fields = RULES["releaseManagementContract"]["diagnosticFields"]
        with mock.patch(
            "scripts.produce_meme_template.production_runtime.doctor",
            return_value={
                "pass": True,
                diagnostic_fields["installSource"]: "/verified-install/4.0.0",
                diagnostic_fields["errorCodes"]: [],
            },
        ):
            result = run_production(
                {**self.request, "productionItemId": "live-wrong-review-method"},
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )
        self.assertEqual([], bucket.put_calls)

    def test_live_p7_rejects_storage_provider_outside_profile(self) -> None:
        adapters, _fal_client, _bucket = build_live_test_adapters(FIXTURE)
        original_upload = adapters.upload
        provider_field = RULES["objectStorageContract"]["adapterResultFields"][
            "provider"
        ]

        def upload_with_wrong_provider(*args):
            upload = original_upload(*args)
            upload[provider_field] = RULES["objectStorageContract"][
                "providerRoles"
            ]["deterministicFixture"]
            return upload

        adapters.upload = upload_with_wrong_provider
        diagnostic_fields = RULES["releaseManagementContract"]["diagnosticFields"]
        with mock.patch(
            "scripts.produce_meme_template.production_runtime.doctor",
            return_value={
                "pass": True,
                diagnostic_fields["installSource"]: "/verified-install/4.0.0",
                diagnostic_fields["errorCodes"]: [],
            },
        ):
            result = run_production(
                {**self.request, "productionItemId": "live-wrong-storage-provider"},
                self.output_root,
                adapters,
                execution_mode=RULES["productionExecutionContract"][
                    "executionModes"
                ]["liveExternal"],
                clock=lambda: FIXED_TIME,
            )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(
            RULES["errorCodes"]["untrustedProductionExecution"],
            result.error_code,
        )


if __name__ == "__main__":
    unittest.main()
