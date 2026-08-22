from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
FIXED_TIME = datetime.fromisoformat("2026-08-21T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class AnalysisTransformAdapters(DeterministicFixtureAdapters):
    def __init__(self, transform):
        super().__init__(FIXTURE)
        self.transform = transform

    def analyze_approved(self, approved_image: Path) -> dict:
        return self.transform(super().analyze_approved(approved_image))


class RejectingIndependentSlotReviewAdapters(DeterministicFixtureAdapters):
    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        result = super().audit_authoring_contract(approved_image, review_request)
        slot_fields = RULES["authoringContractAudit"]["slotReviewFields"]
        reviewed_slot = result[
            RULES["authoringContractAudit"]["reviewFields"]["slotReviews"]
        ][1]
        reviewed_slot[slot_fields["userMotivation"]] = False
        reviewed_slot[slot_fields["evidence"]] = (
            "独立复核发现该项只是批量统一的配色建议，没有当前画面的独立替换动机"
        )
        return result


class IdentityResolutionTransformAdapters(DeterministicFixtureAdapters):
    def __init__(self, transform):
        super().__init__(FIXTURE)
        self.transform = transform

    def resolve_template_identity(
        self, source_image: Path, request: dict
    ) -> dict:
        return self.transform(
            super().resolve_template_identity(source_image, request)
        )


class ProductionReadinessGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tracking_identifier_cannot_be_used_as_new_template_key(self) -> None:
        result = run_production(
            {
                **self.request,
                "productionItemId": "material-1376",
                "templateKey": "material-1376",
            },
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(
            RULES["errorCodes"]["templateKeySemanticInvalid"], result.error_code
        )
        self.assertTrue(
            (self.output_root / "material-1376" / "production-manifest.json").exists()
        )
        self.assertFalse(
            (self.output_root / "material-1376" / "source-analysis.json").exists()
        )

    def test_registry_unavailable_fails_closed_before_source_analysis(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        adapters.resolve_template_identity = None  # type: ignore[method-assign]

        result = run_production(
            {**self.request, "productionItemId": "registry-unavailable"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(
            RULES["errorCodes"]["templateKeyRegistryUnavailable"],
            result.error_code,
        )
        self.assertFalse((result.output_dir / "source-analysis.json").exists())

    def test_existing_key_requires_the_same_registered_source(self) -> None:
        contract = RULES["templateIdentityContract"]
        fields = contract["fields"]

        def mismatched_existing(resolution: dict) -> dict:
            resolution[fields["status"]] = contract["statuses"]["existing"]
            resolution[fields["sourceMatch"]] = False
            return resolution

        result = run_production(
            {**self.request, "productionItemId": "existing-key-mismatch"},
            self.output_root,
            IdentityResolutionTransformAdapters(mismatched_existing),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(
            RULES["errorCodes"]["templateKeyExistingMismatch"],
            result.error_code,
        )

    def test_registered_legacy_key_is_allowed_only_for_the_same_source(self) -> None:
        contract = RULES["templateIdentityContract"]
        fields = contract["fields"]

        def registered_existing(resolution: dict) -> dict:
            resolution[fields["status"]] = contract["statuses"]["existing"]
            resolution[fields["sourceMatch"]] = True
            resolution[fields["semanticKey"]] = False
            return resolution

        result = run_production(
            {
                **self.request,
                "productionItemId": "registered-legacy-key",
                "templateKey": "material-1376",
            },
            self.output_root,
            IdentityResolutionTransformAdapters(registered_existing),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        template = load_json(result.output_dir / "gallery-template.json")
        self.assertEqual("material-1376", template["key"])

    def test_new_key_collision_has_a_dedicated_stop_code(self) -> None:
        fields = RULES["templateIdentityContract"]["fields"]

        def collide(resolution: dict) -> dict:
            resolution[fields["collisionFree"]] = False
            return resolution

        result = run_production(
            {**self.request, "productionItemId": "template-key-collision"},
            self.output_root,
            IdentityResolutionTransformAdapters(collide),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(
            RULES["errorCodes"]["templateKeyConflict"], result.error_code
        )

    def test_production_constraints_cannot_enter_prompt_template(self) -> None:
        def leak_production_constraints(analysis: dict) -> dict:
            analysis["promptTemplate"] += (
                "。保持人物照片的抠图边缘和重复实例裁切，最终只输出平面图案，"
                "不出现领口、袖子、衣服轮廓、褶皱、商品背景、阴影或透视。"
            )
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "prompt-duty-leak"},
            self.output_root,
            AnalysisTransformAdapters(leak_production_constraints),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertTrue(
            (result.output_dir / "authoring-contract-audit.json").exists()
        )
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_paraphrased_product_constraints_are_classified_per_clause(self) -> None:
        def leak_paraphrased_constraint(analysis: dict) -> dict:
            analysis["promptTemplate"] += (
                "。画面限定为单独的二维正面稿，排除所有穿戴载体和陈列场景。"
            )
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "paraphrased-prompt-duty-leak"},
            self.output_root,
            AnalysisTransformAdapters(leak_paraphrased_constraint),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        audit = load_json(result.output_dir / "authoring-contract-audit.json")
        contract = RULES["authoringContractAudit"]
        prompt_review = audit[contract["reviewFields"]["promptReview"]]
        classifications = prompt_review[
            contract["promptReviewFields"]["clauseClassifications"]
        ]
        self.assertTrue(
            any(
                item[contract["promptClauseFields"]["responsibility"]]
                == contract["promptResponsibilities"]["productionConstraint"]
                for item in classifications
            )
        )

    def test_unproven_prompt_clause_fails_closed_without_phrase_match(self) -> None:
        def add_unproven_clause(analysis: dict) -> dict:
            analysis["promptTemplate"] = analysis["promptTemplate"].removesuffix(
                "。"
            ) + (
                "；成品应适配服装印制环节，交付画面需避开实物载体展示。"
            )
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "unproven-prompt-clause"},
            self.output_root,
            AnalysisTransformAdapters(add_unproven_clause),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        audit = load_json(result.output_dir / "authoring-contract-audit.json")
        contract = RULES["authoringContractAudit"]
        classifications = audit[contract["reviewFields"]["promptReview"]][
            contract["promptReviewFields"]["clauseClassifications"]
        ]
        self.assertTrue(
            any(
                item[contract["promptClauseFields"]["responsibility"]]
                == contract["promptResponsibilities"]["unclassified"]
                for item in classifications
            )
        )

    def test_unproven_subclause_cannot_borrow_an_editable_slot_provenance(
        self,
    ) -> None:
        def mix_unproven_content_into_slot_clause(analysis: dict) -> dict:
            analysis["promptTemplate"] = analysis["promptTemplate"].removesuffix(
                "。"
            ) + "，成品应适配服装印制环节。"
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "mixed-unproven-prompt-subclause"},
            self.output_root,
            AnalysisTransformAdapters(mix_unproven_content_into_slot_clause),
            clock=lambda: FIXED_TIME,
        )

        audit = load_json(result.output_dir / "authoring-contract-audit.json")
        contract = RULES["authoringContractAudit"]
        classifications = audit[contract["reviewFields"]["promptReview"]][
            contract["promptReviewFields"]["clauseClassifications"]
        ]
        self.assertTrue(
            any(
                item[contract["promptClauseFields"]["responsibility"]]
                == contract["promptResponsibilities"]["unclassified"]
                for item in classifications
            )
        )
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)

    def test_slot_self_certification_cannot_override_independent_review(self) -> None:
        analysis = load_json(FIXTURE / "approved-analysis.json")
        self.assertTrue(
            all(
                all(slot["valueGates"].values())
                for slot in analysis["slotCandidates"]
            )
        )

        result = run_production(
            {**self.request, "productionItemId": "independent-slot-review"},
            self.output_root,
            RejectingIndependentSlotReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        audit = load_json(result.output_dir / "authoring-contract-audit.json")
        review_fields = RULES["authoringContractAudit"]["reviewFields"]
        slot_fields = RULES["authoringContractAudit"]["slotReviewFields"]
        rejected = [
            review
            for review in audit[review_fields["slotReviews"]]
            if review[slot_fields["userMotivation"]] is False
        ]
        self.assertEqual(1, len(rejected))
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_authoring_audit_is_bound_to_approved_image_and_review_request(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "authoring-audit-binding"},
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        audit = load_json(result.output_dir / "authoring-contract-audit.json")
        review_fields = RULES["authoringContractAudit"]["reviewFields"]
        approved = next(
            (result.output_dir / "evidence").glob("approved-template-image*")
        )
        self.assertEqual(
            hashlib.sha256(approved.read_bytes()).hexdigest(),
            audit[review_fields["approvedImageSha256"]],
        )
        self.assertRegex(
            audit[review_fields["reviewRequestSha256"]], r"^[0-9a-f]{64}$"
        )


if __name__ == "__main__":
    unittest.main()
