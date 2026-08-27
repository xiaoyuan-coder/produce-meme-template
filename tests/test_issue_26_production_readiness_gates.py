from __future__ import annotations

import copy
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


class RejectingTagReviewAdapters(DeterministicFixtureAdapters):
    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        result = super().audit_authoring_contract(approved_image, review_request)
        contract = RULES["authoringContractAudit"]
        review_fields = contract["reviewFields"]
        tag_fields = contract["tagReviewFields"]
        result[review_fields["tagReviews"]][0][
            tag_fields["groundedInApprovedImage"]
        ] = False
        result[review_fields["tagReviews"]][0][tag_fields["evidence"]] = (
            "独立复核发现该标签无法从当前 Approved Image 核验"
        )
        return result


class RejectingIdentityInheritanceReviewAdapters(DeterministicFixtureAdapters):
    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        result = super().audit_authoring_contract(approved_image, review_request)
        contract = RULES["authoringContractAudit"]
        review_fields = contract["reviewFields"]
        inheritance_fields = contract["identityInheritanceReviewFields"]
        review = result[review_fields["identityInheritanceReviews"]][0]
        review[inheritance_fields["clothingPolicyValid"]] = False
        review[inheritance_fields["evidence"]] = (
            "独立对照 Approved Image 后发现可见服装没有跟随用户上传图"
        )
        return result


class RejectingDefaultValueSimplicityReviewAdapters(DeterministicFixtureAdapters):
    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        result = super().audit_authoring_contract(approved_image, review_request)
        contract = RULES["authoringContractAudit"]
        review_fields = contract["reviewFields"]
        default_fields = contract["defaultValueReviewFields"]
        review = result[review_fields["defaultValueReviews"]][0]
        review[default_fields["minimalWording"]] = False
        review[default_fields["evidence"]] = (
            "默认值可以进一步缩短且不损失当前编辑轴"
        )
        return result


class RejectingVisibleCopyReviewAdapters(DeterministicFixtureAdapters):
    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict
    ) -> dict:
        result = super().audit_authoring_contract(approved_image, review_request)
        contract = RULES["authoringContractAudit"]
        review_fields = contract["reviewFields"]
        copy_fields = contract["copyReviewFields"]
        review = result[review_fields["copyReview"]]
        review[copy_fields["descriptionGrounded"]] = False
        review[copy_fields["evidence"]] = (
            "描述加入了 Approved Image 无法核验的场景或身份事实"
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


class ExistingTemplateAnalysisTransformAdapters(AnalysisTransformAdapters):
    def resolve_template_identity(
        self, source_image: Path, request: dict
    ) -> dict:
        result = super().resolve_template_identity(source_image, request)
        contract = RULES["templateIdentityContract"]
        fields = contract["fields"]
        result[fields["status"]] = contract["statuses"]["existing"]
        result[fields["sourceMatch"]] = True
        return result


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
                "preservedTitle": "软垫上的困倦瞬间",
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

    def test_tags_require_per_image_grounding_and_independent_review(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "independent-tag-review"},
            self.output_root,
            RejectingTagReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_identity_inheritance_requires_independent_approved_image_review(
        self,
    ) -> None:
        result = run_production(
            {**self.request, "productionItemId": "identity-inheritance-review"},
            self.output_root,
            RejectingIdentityInheritanceReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertTrue(
            (result.output_dir / "authoring-contract-audit.json").exists()
        )
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_default_values_require_independent_minimal_wording_review(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "default-value-simplicity"},
            self.output_root,
            RejectingDefaultValueSimplicityReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertTrue(
            (result.output_dir / "authoring-contract-audit.json").exists()
        )
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_visible_text_routing_is_a_critical_delivery_outcome(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "visible-text-critical-outcome"},
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        qualification = load_json(
            result.output_dir / RULES["criticalOutcomeContract"]["artifactName"]
        )
        identity_field = RULES["criticalOutcomeContract"][
            "requirementResultFields"
        ]["identity"]
        requirement_ids = RULES["criticalOutcomeContract"]["requirementIds"]
        self.assertTrue(
            {
                requirement_ids["identityInheritancePolicy"],
                requirement_ids["conciseSlotDefaults"],
                requirement_ids["visibleTextRouting"],
            }.issubset(
                {
                    item[identity_field]
                    for item in qualification["requirements"]
                    if item["pass"] is True
                }
            )
        )

    def test_title_and_description_require_independent_image_grounding(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "visible-copy-grounding"},
            self.output_root,
            RejectingVisibleCopyReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_generic_batch_tags_are_blocked_before_formal_compilation(self) -> None:
        def reuse_generic_tags(analysis: dict) -> dict:
            analysis["tags"] = ["动物", "热门", "好看", "宠物", "蜷卧"]
            analysis["tagGroundingEvidence"] = [
                {
                    "tag": tag,
                    "evidence": f"当前确认图的可见内容支持标签「{tag}」的分类价值",
                }
                for tag in analysis["tags"]
            ]
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "generic-reused-tags"},
            self.output_root,
            AnalysisTransformAdapters(reuse_generic_tags),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_tag_grounding_evidence_rejects_duplicate_rows(self) -> None:
        def duplicate_tag_evidence(analysis: dict) -> dict:
            analysis["tagGroundingEvidence"].append(
                copy.deepcopy(analysis["tagGroundingEvidence"][0])
            )
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "duplicate-tag-evidence"},
            self.output_root,
            AnalysisTransformAdapters(duplicate_tag_evidence),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_tags_require_five_to_eight_items_and_one_big_category(self) -> None:
        def five_tags_without_category(analysis: dict) -> dict:
            analysis["tags"] = ["宠物", "蜷卧", "软垫", "侧面光线", "治愈氛围"]
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "tags-without-big-category"},
            self.output_root,
            AnalysisTransformAdapters(five_tags_without_category),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_description_over_twenty_characters_is_blocked(self) -> None:
        def use_long_description(analysis: dict) -> dict:
            analysis["neutralDescription"] = "这是一条超过二十个字符并且罗列了很多无关细节的模板描述"
            return analysis

        result = run_production(
            {**self.request, "productionItemId": "description-too-long"},
            self.output_root,
            AnalysisTransformAdapters(use_long_description),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_stored_recompilation_cannot_rewrite_preserved_title(self) -> None:
        def rewrite_stored_title(analysis: dict) -> dict:
            analysis["neutralTitle"] = "重新分析后的新标题"
            return analysis

        result = run_production(
            {
                **self.request,
                "productionItemId": "stored-title-rewritten",
                "preservedTitle": "旧正式标题",
            },
            self.output_root,
            ExistingTemplateAnalysisTransformAdapters(rewrite_stored_title),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(RULES["errorCodes"]["riskNeedsReview"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_stored_recompilation_without_preserved_title_needs_review(self) -> None:
        contract = RULES["templateIdentityContract"]
        fields = contract["fields"]

        def registered_existing(resolution: dict) -> dict:
            resolution[fields["status"]] = contract["statuses"]["existing"]
            resolution[fields["sourceMatch"]] = True
            return resolution

        result = run_production(
            {**self.request, "productionItemId": "stored-title-missing"},
            self.output_root,
            IdentityResolutionTransformAdapters(registered_existing),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(RULES["errorCodes"]["riskNeedsReview"], result.error_code)
        self.assertFalse((result.output_dir / "source-analysis.json").exists())

    def test_new_template_cannot_claim_a_preserved_title(self) -> None:
        result = run_production(
            {
                **self.request,
                "productionItemId": "new-template-with-preserved-title",
                "preservedTitle": "软垫上的困倦瞬间",
            },
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(RULES["errorCodes"]["riskNeedsReview"], result.error_code)
        self.assertFalse((result.output_dir / "source-analysis.json").exists())

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
