from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


RULES = load_json(ROOT / "contracts" / "machine-rules.json")
PHASES = RULES["productionPhases"]
PHASE_NAMES = [item["phase"] for item in PHASES]
RESULT_COMPLETED, RESULT_NEEDS_INPUT, RESULT_BLOCKED, RESULT_FAILED = RULES["resultStates"]
ERROR_CODES = RULES["errorCodes"]


class Issue2VerticalSliceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_fixture(self, adapters=None, request=None):
        adapters = adapters or DeterministicFixtureAdapters(FIXTURE)
        result = run_production(request or self.request, self.output_root, adapters, clock=lambda: FIXED_TIME)
        return result, adapters

    def test_e01_single_call_keeps_independent_state_lineage_and_pin(self) -> None:
        result, adapters = self.run_fixture()

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        self.assertEqual(RULES["resultStates"][RESULT_COMPLETED], result.state)
        item = result.output_dir
        manifest = load_json(item / "production-manifest.json")
        pin = load_json(item / "production-pin.json")
        release = load_json(ROOT / "release.json")
        self.assertEqual(PHASE_NAMES, [entry["phase"] for entry in manifest["history"]])
        self.assertEqual(1, manifest["revision"])
        self.assertEqual(release["skillVersion"], pin["skill"]["version"])
        self.assertEqual(release["artifactSchemaVersion"], pin["artifactSchemaVersion"])
        self.assertEqual(64, len(pin["galleryContract"]["sha256"]))
        self.assertEqual(64, len(pin["releaseSha256"]))
        release_json_sha = hashlib.sha256((ROOT / "release.json").read_bytes()).hexdigest()
        tracked_count = len(load_json(ROOT / "skill-manifest.json")["tracked_files"])
        self.assertNotEqual(release_json_sha, pin["releaseSha256"])
        self.assertEqual(tracked_count, pin["releaseFileCount"])
        self.assertEqual(64, len(pin["releaseManifestSha256"]))
        for name, artifact in manifest["artifacts"].items():
            self.assertTrue((item / name).is_file())
            self.assertEqual(64, len(artifact["sha256"]))
            self.assertEqual(1, artifact["revision"])
        self.assertEqual(1, len(adapters.generate_calls))
        self.assertEqual(1, adapters.generate_calls[0]["imageCount"])
        self.assertEqual(1, len(adapters.upload_calls))

    def test_e05_e07_autonomous_plan_has_one_same_category_target_and_closure(self) -> None:
        result, _ = self.run_fixture()
        plan = load_json(result.output_dir / "replacement-plan.json")

        self.assertEqual(RULES["strategySources"]["autonomousDecision"], plan["strategy"]["source"])
        self.assertEqual(1, len(plan["primaryTargets"]))
        target = plan["primaryTargets"][0]
        self.assertEqual(target["sourceCategory"], target["replacementCategory"])
        self.assertEqual("柯基犬", target["replacementValue"])
        self.assertEqual(1, sum(item["kind"] == "primary" for item in plan["changedSet"]))
        self.assertEqual(
            {"full_body", "repeated_instance", "shadow", "identity_mark"},
            {item["type"] for item in plan["dependencyClosure"]},
        )

    def test_e10_e11_hard_visual_failure_cannot_upload_or_be_approved(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE).with_visual_review(
            {"hardGates": {"watermarkFree": False}}
        )
        request = {**self.request, "productionItemId": "hard-visual-failure"}
        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(ERROR_CODES["visualHardFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)
        self.assertTrue((result.output_dir / "evidence" / "generated-candidate-image.ppm").exists())
        self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
        self.assertFalse((result.output_dir / "template-analysis.json").exists())
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_e04_source_identity_leak_blocks_before_upload(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original = adapters.analyze_approved

        def leaking_analysis(path):
            analysis = original(path)
            analysis["neutralTitle"] = "橘猫的困倦瞬间"
            return analysis

        adapters.analyze_approved = leaking_analysis
        request = {**self.request, "productionItemId": "source-leak"}
        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(ERROR_CODES["contractFailure"], result.error_code)
        report = load_json(result.output_dir / "validation-report.json")
        self.assertEqual(["橘猫"], report["layers"]["semantic"]["evidence"]["sourceLeaks"])
        self.assertEqual([], adapters.upload_calls)

    def test_hidden_constraints_cannot_lock_prompt_slot_or_free_content(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original = adapters.analyze_approved

        def conflicting_analysis(path):
            analysis = original(path)
            analysis["promptEnhancement"]["lockedConstraints"].append("承托垫的色调维持暖色系")
            return analysis

        adapters.analyze_approved = conflicting_analysis
        request = {**self.request, "productionItemId": "hidden-open-content-conflict"}

        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(ERROR_CODES["contractFailure"], result.error_code)
        report = load_json(result.output_dir / "validation-report.json")
        self.assertFalse(report["layers"]["semantic"]["evidence"]["semanticAudit"]["contentBound"])
        self.assertEqual([], adapters.upload_calls)

    def test_prompt_must_include_free_content_and_resolve_as_natural_language(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original = adapters.analyze_approved

        def incomplete_prompt(path):
            analysis = original(path)
            analysis["promptTemplate"] = (
                "一只{{ pet_subject }}狗蜷卧在{{ cushion_look }}垫子上，前爪搭住垫边，"
                "{{ room_mood }}光线从侧面照入，背景保留安静的客厅和轻微景深。"
            )
            return analysis

        adapters.analyze_approved = incomplete_prompt
        request = {**self.request, "productionItemId": "incomplete-prompt-template"}

        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        report = load_json(result.output_dir / "validation-report.json")
        semantic_evidence = report["layers"]["semantic"]["evidence"]
        self.assertEqual([], semantic_evidence["missingFreeEditableContent"])
        self.assertFalse(semantic_evidence["semanticAudit"]["contentBound"])
        self.assertEqual([], adapters.upload_calls)

    def test_title_must_survive_maximum_difference_subject_input(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original = adapters.analyze_approved

        def identity_bound_title(path):
            analysis = original(path)
            analysis["neutralTitle"] = "汪星人的困倦写真"
            return analysis

        adapters.analyze_approved = identity_bound_title
        request = {**self.request, "productionItemId": "identity-bound-title"}

        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(ERROR_CODES["contractFailure"], result.error_code)
        report = load_json(result.output_dir / "validation-report.json")
        self.assertFalse(report["layers"]["semantic"]["evidence"]["semanticAudit"]["contentBound"])
        self.assertEqual([], adapters.upload_calls)

    def test_e19_e21_e27_approved_image_drives_three_high_value_slots_and_prompt(self) -> None:
        result, _ = self.run_fixture()
        approved = result.output_dir / "evidence" / "approved-template-image.ppm"
        analysis = load_json(result.output_dir / "template-analysis.json")
        editable = load_json(result.output_dir / "editable-template-spec.json")
        semantic_audit = load_json(result.output_dir / "semantic-audit.json")

        self.assertEqual(hashlib.sha256(approved.read_bytes()).hexdigest(), analysis["visualFactSourceSha256"])
        self.assertEqual(3, len(editable["slots"]))
        self.assertIn("subject", {slot["semanticRole"] for slot in editable["slots"]})
        self.assertTrue(all(all(slot["valueGates"].values()) for slot in editable["slots"]))
        self.assertEqual({"pet_subject", "cushion_look", "room_mood"}, set(editable["slotSuggestionPools"]))
        for slot_id in editable["slotSuggestionPools"]:
            self.assertIn("{{ " + slot_id, editable["promptTemplate"])
        self.assertIn("安静的客厅", editable["freeEditableContent"])
        self.assertTrue(all(semantic_audit["checks"].values()))

    def test_e35_e36_formal_projection_is_clean_and_uses_one_uploaded_asset(self) -> None:
        result, adapters = self.run_fixture()
        record = load_json(result.gallery_template)
        rules = load_json(ROOT / "contracts" / "machine-rules.json")

        self.assertEqual(set(rules["formalProjection"]["topLevel"]), set(record))
        self.assertEqual({"tags"}, set(record["metadata"]))
        self.assertEqual(record["cover"], record["referenceImage"])
        self.assertNotIn("coverUrl", json.dumps(record, ensure_ascii=False))
        self.assertNotIn("replacementPool", json.dumps(record, ensure_ascii=False))
        self.assertNotIn("visualDimensions", json.dumps(record, ensure_ascii=False))
        uploaded = Path(adapters.upload_calls[0]["approvedImage"])
        self.assertEqual(result.output_dir / "evidence" / "approved-template-image.ppm", uploaded)
        self.assertTrue(load_json(result.output_dir / "final-validation-report.json")["pass"])

    def test_repeat_is_idempotent_and_reuses_finalized_item(self) -> None:
        first, _ = self.run_fixture()
        second_adapters = DeterministicFixtureAdapters(FIXTURE)
        second, second_adapters = self.run_fixture(second_adapters)

        self.assertEqual(first.production_item_id, second.production_item_id)
        self.assertTrue(second.resumed)
        self.assertEqual([], second_adapters.generate_calls)
        self.assertEqual([], second_adapters.upload_calls)

    def test_external_adapter_failure_has_stable_failed_result(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)

        def fail_generation(*_args):
            raise TimeoutError("fixture timeout")

        adapters.generate = fail_generation
        request = {**self.request, "productionItemId": "adapter-failure"}
        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_FAILED, result.outcome)
        self.assertEqual(RULES["resultStates"][RESULT_FAILED], result.state)
        self.assertEqual(ERROR_CODES["externalFailure"], result.error_code)
        manifest = load_json(result.output_dir / "production-manifest.json")
        self.assertEqual("generate", manifest["error"]["evidence"]["operation"])
        self.assertEqual([], adapters.upload_calls)

    def test_unsafe_identifiers_are_rejected_before_any_output(self) -> None:
        unsafe_root = self.output_root / "allowed"
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {**self.request, "templateKey": "../escaped"}

        result = run_production(request, unsafe_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_NEEDS_INPUT, result.outcome)
        self.assertEqual(ERROR_CODES["invalidProductionRequest"], result.error_code)
        self.assertFalse((self.output_root / "escaped-b24c23d936ef").exists())
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_generation_adapter_extension_cannot_escape_evidence_directory(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original = adapters.generate

        def unsafe_extension(*args):
            generated = original(*args)
            generated["extension"] = "/../../../escaped.ppm"
            return generated

        adapters.generate = unsafe_extension
        request = {**self.request, "productionItemId": "unsafe-generated-extension"}

        result, adapters = self.run_fixture(adapters, request)

        self.assertEqual(RESULT_FAILED, result.outcome)
        self.assertEqual(ERROR_CODES["externalFailure"], result.error_code)
        self.assertFalse((self.output_root / "escaped.ppm").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_existing_item_symlink_cannot_escape_output_root(self) -> None:
        output_root = self.output_root / "allowed"
        outside = self.output_root / "outside"
        output_root.mkdir()
        outside.mkdir()
        (output_root / "symlinked-production-item").symlink_to(outside, target_is_directory=True)
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {**self.request, "productionItemId": "symlinked-production-item"}

        result = run_production(request, output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_NEEDS_INPUT, result.outcome)
        self.assertEqual(ERROR_CODES["invalidProductionRequest"], result.error_code)
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual([], adapters.generate_calls)

    def test_completed_item_with_missing_delivery_fails_integrity_check(self) -> None:
        first, _ = self.run_fixture()
        first.gallery_template.unlink()
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed, adapters = self.run_fixture(adapters)

        self.assertEqual(RESULT_BLOCKED, resumed.outcome)
        self.assertEqual(ERROR_CODES["productionItemIntegrityFailure"], resumed.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_completed_item_cannot_be_reused_for_another_template_key(self) -> None:
        first_request = {**self.request, "productionItemId": "shared-production-item"}
        self.run_fixture(request=first_request)
        adapters = DeterministicFixtureAdapters(FIXTURE)
        conflicting_request = {
            **self.request,
            "productionItemId": "shared-production-item",
            "templateKey": "another-template-key",
        }

        resumed, adapters = self.run_fixture(adapters, conflicting_request)

        self.assertEqual(RESULT_BLOCKED, resumed.outcome)
        self.assertEqual(ERROR_CODES["productionItemIntegrityFailure"], resumed.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_recovery_after_upload_reuses_asset_receipt(self) -> None:
        first, _ = self.run_fixture()
        item = first.output_dir
        receipt_before = (item / "asset-receipt.json").read_bytes()
        manifest = load_json(item / "production-manifest.json")
        uploaded_phase = PHASES[7]
        manifest["phase"] = uploaded_phase["phase"]
        manifest["state"] = uploaded_phase["state"]
        manifest["outcome"] = None
        manifest["history"] = [entry for entry in manifest["history"] if entry["phase"] != PHASES[8]["phase"]]
        manifest["artifacts"].pop("final-validation-report.json")
        manifest["artifacts"].pop("gallery-template.json")
        (item / "final-validation-report.json").unlink()
        (item / "gallery-template.json").unlink()
        (item / "production-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed, adapters = self.run_fixture(adapters)

        self.assertEqual(RESULT_COMPLETED, resumed.outcome)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertEqual(receipt_before, (item / "asset-receipt.json").read_bytes())
        self.assertTrue((item / "gallery-template.json").is_file())


if __name__ == "__main__":
    unittest.main()
