from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Callable

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production
from tests.fixture_contracts import (
    author_explicit_slot_suggestion_reviews,
    rebuild_approved_component_graph,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
UNSEEN_FORWARD_FIXTURE = ROOT / "fixtures" / "shadow-release" / "unseen-forward"
RULES = json.loads((ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8"))
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")
SLOT_CONTRACT = RULES["slotCompilationContract"]
VALUE_GATE_ROLES = SLOT_CONTRACT["valueGateRoles"]
PERSON_KIND = SLOT_CONTRACT["subjectKinds"]["humanSubject"]
NON_PERSON_KIND = SLOT_CONTRACT["subjectKinds"]["nonHumanSubject"]
SUBJECT_ROLE = SLOT_CONTRACT["semanticRoles"]["primarySubject"]
SUBJECT_TYPE = SLOT_CONTRACT["slotTypes"]["primarySubjectUpload"]
SUBJECT_PROMPT_VALUE_FIELD, SUBJECT_HINT_FIELD = tuple(
    SLOT_CONTRACT["imageInputOptionalAuthoringFields"].values()
)
SUBJECT_DEFAULTS = SLOT_CONTRACT["subjectInputDefaults"]
PERSON_ATTRIBUTE_ROLES = tuple(SLOT_CONTRACT["personAttributeRoles"].values())
TRAIT_KINDS = SLOT_CONTRACT["identityInheritanceDecision"]["traitKinds"]
IDENTITY_TRAIT_KIND, CLOTHING_TRAIT_KIND, OTHER_TRAIT_KIND = tuple(
    TRAIT_KINDS.values()
)
SINGLE_SLOT_REVIEW_AXES = tuple(SLOT_CONTRACT["singleSlotReviewAxes"].values())
ASSET_COUNT_FIELDS = SLOT_CONTRACT["assetUnitCountFields"]
VISIBLE_TEXT_SLOT_TYPE = SLOT_CONTRACT["slotTypes"]["visibleTextPrompt"]
TEXT_CONTRACT = RULES["visibleTextContract"]
TEXT_ANALYSIS_FIELDS = TEXT_CONTRACT["analysisFields"]
TEXT_INVENTORY_FIELDS = TEXT_CONTRACT["inventoryFields"]
TEXT_REGION_FIELDS = TEXT_CONTRACT["regionFields"]
TEXT_EVIDENCE_FIELDS = TEXT_CONTRACT["exactEvidenceFields"]
TEXT_ROLES = TEXT_CONTRACT["roles"]
TEXT_ACTIONS = TEXT_CONTRACT["actions"]
TEXT_VALUE_CLASSES = TEXT_CONTRACT["valueClasses"]
TEXT_LANGUAGES = TEXT_CONTRACT["languageValues"]
FORBIDDEN_VISUAL_REFERENCE_FRAGMENTS = RULES["runtimeSemanticsContract"][
    "visualGrounding"
]["forbiddenReferenceOnlyFragments"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def add_unified_rendering_decision(
    analysis: dict,
    *,
    medium: str = "统一的二维数字平涂插画，所有前景与背景对象使用同一闭合线稿体系",
    style_traits: list[str] | None = None,
) -> dict:
    style_traits = style_traits or [
        "统一使用清晰闭合外轮廓、低渐变色块和少量平面高光，避免局部写实摄影质感"
    ]
    component_ids = [
        component["componentId"]
        for component in analysis["componentGraph"]["components"]
    ]
    subject = next(
        slot for slot in analysis["slotCandidates"] if slot["type"] == SUBJECT_TYPE
    )
    subject_target_ids = [
        target["id"]
        for target in analysis["runtimeSemantics"]["targetInstances"]
        if target["kind"] == "identity_subject"
    ]
    analysis["renderingCoherenceDecision"] = {
        "mode": "unified",
        "approvedImageSha256": analysis["visualFactSourceSha256"],
        "medium": medium,
        "renderingUnits": [
            {
                "unitId": "whole-approved-image",
                "componentIds": component_ids,
                "styleTraits": style_traits,
                "evidence": "确认模板图中主体、承托物、接触阴影和背景共享闭合线稿与平涂色块",
            }
        ],
        "boundaryEvidence": [],
        "subjectTransfers": [
            {
                "inputId": subject["id"],
                "targetIds": subject_target_ids,
                "inheritFromUpload": subject["identityInheritanceDecision"][
                    "inheritFromUpload"
                ],
                "keepFromTemplate": subject["identityInheritanceDecision"][
                    "keepFromTemplate"
                ],
                "renderingUnitId": "whole-approved-image",
                "completeRedraw": True,
                "evidence": "上传主体只继承身份范围，并完整重绘进确认模板图的统一二维媒介",
            }
        ],
        "evidence": "逐组件检查后确认整张图只有一个渲染体系",
    }
    return analysis


class ApprovedAnalysisAdapters(DeterministicFixtureAdapters):
    def __init__(self, transform: Callable[[dict], dict]):
        super().__init__(FIXTURE)
        self.transform = transform

    def analyze_approved(self, approved_image: Path) -> dict:
        analysis = self.transform(super().analyze_approved(approved_image))
        if "assetUnitAnalysis" in analysis:
            rebuild_approved_component_graph(analysis, RULES)
        return analysis

    def audit_semantics(self, content: dict) -> dict:
        result = super().audit_semantics(content)
        author_explicit_slot_suggestion_reviews(result, content, RULES)
        digest = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result["contentSha256"] = digest
        result["observedContentSha256"] = digest
        return result


class PostRebuildApprovedAnalysisAdapters(ApprovedAnalysisAdapters):
    def analyze_approved(self, approved_image: Path) -> dict:
        analysis = DeterministicFixtureAdapters.analyze_approved(
            self, approved_image
        )
        rebuild_approved_component_graph(analysis, RULES)
        return self.transform(analysis)


class Issue5EditablePromptCompilerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_case(self, item_id: str, transform: Callable[[dict], dict]):
        return run_production(
            {**self.request, "productionItemId": item_id},
            self.output_root,
            ApprovedAnalysisAdapters(transform),
            clock=lambda: FIXED_TIME,
        )

    def test_audited_subject_omission_can_keep_two_other_high_value_slots(self) -> None:
        def omit_subject(analysis: dict) -> dict:
            analysis["slotCandidates"] = [
                slot for slot in analysis["slotCandidates"] if slot["semanticRole"] != SUBJECT_ROLE
            ]
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                '{{ pet_subject | "柯基犬" }}',
                "一只放松的小动物",
            ).replace("一只一只", "一只")
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    role: role != VALUE_GATE_ROLES["mechanismPreservation"]
                    for role in VALUE_GATE_ROLES.values()
                },
                "uploadReplacementFeasible": False,
                "blockerCode": "fixed_identity_is_mechanism_anchor",
                "evidence": "主体替换会破坏唯一的伏卧接触机制",
            }
            analysis["assetUnitAnalysis"][ASSET_COUNT_FIELDS["controls"]] = 2
            analysis["assetUnitAnalysis"][ASSET_COUNT_FIELDS["uploads"]] = 0
            analysis["renderingCoherenceDecision"]["subjectTransfers"] = []
            return analysis

        result = self.run_case("audited-subject-slot-omission", omit_subject)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        self.assertEqual(2, len(editable["slots"]))
        self.assertEqual(
            "主体替换会破坏唯一的伏卧接触机制",
            editable["subjectSlotOmissionEvidence"]["evidence"],
        )

    def test_single_identity_cannot_claim_inseparable_multi_identity_omission(self) -> None:
        def forge_multi_identity_blocker(analysis: dict) -> dict:
            analysis["slotCandidates"] = [
                slot
                for slot in analysis["slotCandidates"]
                if slot["semanticRole"] != SUBJECT_ROLE
            ]
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                '{{ pet_subject | "柯基犬" }}',
                "一只放松的小动物",
            ).replace("一只一只", "一只")
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    role: role != VALUE_GATE_ROLES["mechanismPreservation"]
                    for role in VALUE_GATE_ROLES.values()
                },
                "uploadReplacementFeasible": False,
                "blockerCode": "inseparable_multi_identity_unit",
                "evidence": "声称多身份不可分，但资产分析只有一个身份单元",
            }
            analysis["assetUnitAnalysis"][ASSET_COUNT_FIELDS["controls"]] = 2
            analysis["assetUnitAnalysis"][ASSET_COUNT_FIELDS["uploads"]] = 0
            analysis["renderingCoherenceDecision"]["subjectTransfers"] = []
            return analysis

        result = self.run_case(
            "forged-inseparable-multi-identity-omission",
            forge_multi_identity_blocker,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_exhaustive_single_slot_exception_is_allowed_and_preserved_as_evidence(self) -> None:
        reviewed_axes = list(SINGLE_SLOT_REVIEW_AXES)

        def one_slot(analysis: dict) -> dict:
            analysis["slotCandidates"] = [analysis["slotCandidates"][0]]
            analysis["promptTemplate"] = (
                '一只{{ pet_subject | "柯基犬" }}蜷卧在柔软承托物上，前爪搭住边缘，'
                "侧面柔光照入安静室内，背景带轻微景深。"
            )
            analysis["freeEditableContent"] = ["柔软承托物", "侧面柔光", "安静室内", "轻微景深"]
            analysis["singleSlotExceptionEvidence"] = {
                "confirmedOnlyOneHighValue": True,
                "reviewedAxes": reviewed_axes,
                "reason": "其余内容缺少独立用户动机或会破坏核心机制",
            }
            analysis["assetUnitAnalysis"][ASSET_COUNT_FIELDS["controls"]] = 1
            return analysis

        result = self.run_case("audited-single-slot-exception", one_slot)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        self.assertEqual(1, len(editable["slots"]))
        self.assertEqual(set(reviewed_axes), set(editable["singleSlotExceptionEvidence"]["reviewedAxes"]))

    def test_long_production_style_default_value_is_rejected(self) -> None:
        def long_default(analysis: dict) -> dict:
            analysis["slotCandidates"][1]["defaultValue"] = "带有复杂编织纹理的暖黄色柔软大坐垫"
            return analysis

        adapters = ApprovedAnalysisAdapters(long_default)
        result = run_production(
            {**self.request, "productionItemId": "long-slot-default-value"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_preferred_default_length_requires_audited_exception(self) -> None:
        def one_character_default(analysis: dict) -> dict:
            analysis["slotCandidates"][0]["defaultValue"] = "犬"
            analysis["promptTemplate"] = analysis["promptTemplate"].replace("柯基犬", "犬")
            return analysis

        blocked = self.run_case("one-character-default-without-evidence", one_character_default)

        self.assertEqual(RULES["resultStates"]["blocked"], blocked.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], blocked.error_code)
        self.assertFalse((blocked.output_dir / "editable-template-spec.json").exists())

        def audited_one_character_default(analysis: dict) -> dict:
            analysis = one_character_default(analysis)
            slot_id = analysis["slotCandidates"][0]["id"]
            analysis["defaultValuePreferenceExceptionEvidence"] = {
                slot_id: {
                    "reviewed": True,
                    "reason": "单字犬是该可见主体的最短自然中文称呼",
                }
            }
            return analysis

        completed = self.run_case("audited-one-character-default", audited_one_character_default)

        self.assertEqual(RULES["resultStates"]["completed"], completed.state)
        editable = load_json(completed.output_dir / "editable-template-spec.json")
        slot_id = editable["slots"][0]["id"]
        self.assertIn(slot_id, editable["defaultValuePreferenceExceptionEvidence"])

    def test_non_chinese_default_requires_audited_exception(self) -> None:
        def english_default(analysis: dict) -> dict:
            analysis["slotCandidates"][0]["defaultValue"] = "cat"
            analysis["promptTemplate"] = analysis["promptTemplate"].replace("柯基犬", "cat")
            return analysis

        result = self.run_case("english-default-without-evidence", english_default)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_editable_sidecar_exposes_one_resolved_prompt_for_both_edit_modes(self) -> None:
        result = run_production(
            {**self.request, "productionItemId": "unified-resolved-prompt"},
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        contract = editable["resolvedPromptContract"]
        self.assertEqual("promptTemplate", contract["singleSourceField"])
        self.assertEqual(
            {slot["id"]: slot["defaultValue"] for slot in editable["slots"]},
            contract["defaultSlotValues"],
        )
        self.assertNotIn("{{", contract["defaultResolvedPrompt"])
        self.assertTrue(
            all(value in contract["defaultResolvedPrompt"] for value in editable["freeEditableContent"])
        )

    def test_person_attributes_require_independent_value_and_stability_assessments(self) -> None:
        def missing_assessments(analysis: dict) -> dict:
            analysis["subjectKind"] = PERSON_KIND
            return analysis

        missing = self.run_case("person-attributes-missing-assessment", missing_assessments)

        self.assertEqual(RULES["resultStates"]["blocked"], missing.state)
        self.assertFalse((missing.output_dir / "editable-template-spec.json").exists())

        def assessed(analysis: dict) -> dict:
            analysis["subjectKind"] = PERSON_KIND
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"]["clothingVisible"] = False
            analysis["subjectAttributeAssessments"] = {
                role: {gate: False for gate in VALUE_GATE_ROLES.values()} | {
                    "includedAsSlot": False,
                    "evidence": f"已独立检查 {role} 的编辑价值与生成稳定性",
                }
                for role in PERSON_ATTRIBUTE_ROLES
            }
            return analysis

        valid = self.run_case("person-attributes-independently-assessed", assessed)

        self.assertEqual(RULES["resultStates"]["completed"], valid.state)
        editable = load_json(valid.output_dir / "editable-template-spec.json")
        self.assertEqual(set(PERSON_ATTRIBUTE_ROLES), set(editable["subjectAttributeAssessments"]))

    def test_subject_identity_inheritance_scope_compiles_into_runtime_relations_only(self) -> None:
        def decide_inheritance(analysis: dict) -> dict:
            subject = next(
                slot for slot in analysis["slotCandidates"] if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"] = {
                "clothingVisible": False,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                    "毛色与花纹": OTHER_TRAIT_KIND,
                    "表情": OTHER_TRAIT_KIND,
                    "蜷卧姿态与前爪搭住软垫的动作": OTHER_TRAIT_KIND,
                },
                "inheritFromUpload": ["可辨认身份特征", "毛色与花纹", "表情"],
                "keepFromTemplate": ["蜷卧姿态与前爪搭住软垫的动作"],
                "reason": "蜷卧和搭爪动作构成模板核心玩法",
            }
            return analysis

        result = self.run_case("subject-identity-inheritance-scope", decide_inheritance)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        formal = load_json(result.gallery_template)
        editable_subject = next(
            slot for slot in editable["slots"] if slot["type"] == SUBJECT_TYPE
        )
        formal_subject = next(
            slot for slot in formal["inputSchema"]["slots"] if slot["id"] == "pet_subject"
        )
        self.assertEqual(
            {
                "clothingVisible": False,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                    "毛色与花纹": OTHER_TRAIT_KIND,
                    "表情": OTHER_TRAIT_KIND,
                    "蜷卧姿态与前爪搭住软垫的动作": OTHER_TRAIT_KIND,
                },
                "inheritFromUpload": ["可辨认身份特征", "毛色与花纹", "表情"],
                "keepFromTemplate": ["蜷卧姿态与前爪搭住软垫的动作"],
                "reason": "蜷卧和搭爪动作构成模板核心玩法",
            },
            editable_subject["identityInheritanceDecision"],
        )
        self.assertNotIn("identityInheritanceDecision", formal_subject)
        self.assertIn(
            "图片模式下，输入 pet_subject 的可辨认身份特征、毛色与花纹、表情读取用户上传图，并按模板媒介重绘",
            formal["runtimeSemantics"]["visualContract"]["relations"],
        )
        self.assertIn(
            "输入 pet_subject 的蜷卧姿态与前爪搭住软垫的动作沿用模板角色位；蜷卧和搭爪动作构成模板核心玩法",
            formal["runtimeSemantics"]["visualContract"]["relations"],
        )

    def test_subject_can_inherit_all_identity_traits_without_a_fixed_exception_reason(
        self,
    ) -> None:
        def inherit_all(analysis: dict) -> dict:
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"] = {
                "clothingVisible": False,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                    "毛色与花纹": OTHER_TRAIT_KIND,
                    "表情与动作": OTHER_TRAIT_KIND,
                },
                "inheritFromUpload": [
                    "可辨认身份特征",
                    "毛色与花纹",
                    "表情与动作",
                ],
                "keepFromTemplate": [],
                "reason": "",
            }
            transfer = analysis["renderingCoherenceDecision"]["subjectTransfers"][0]
            transfer["inheritFromUpload"] = subject["identityInheritanceDecision"][
                "inheritFromUpload"
            ]
            transfer["keepFromTemplate"] = []
            return analysis

        result = self.run_case("subject-inherits-all-identity-traits", inherit_all)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        formal = load_json(result.gallery_template)
        relations = formal["runtimeSemantics"]["visualContract"]["relations"]
        self.assertIn(
            "图片模式下，输入 pet_subject 的可辨认身份特征、毛色与花纹、表情与动作读取用户上传图，并按模板媒介重绘",
            relations,
        )
        self.assertFalse(any("沿用模板角色位" in value for value in relations))

    def test_clothing_defaults_to_upload_and_only_core_play_feature_is_fixed(
        self,
    ) -> None:
        def visible_clothing_omitted(analysis: dict) -> dict:
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"]["clothingVisible"] = True
            return analysis

        omitted = self.run_case(
            "visible-non-person-clothing-cannot-be-omitted",
            visible_clothing_omitted,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], omitted.state)

        def core_clothing_feature(analysis: dict) -> dict:
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"] = {
                "clothingVisible": True,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                    "服装颜色与材质": CLOTHING_TRAIT_KIND,
                    "表情": OTHER_TRAIT_KIND,
                    "撑起夸张斗篷轮廓的服装结构": CLOTHING_TRAIT_KIND,
                },
                "inheritFromUpload": [
                    "可辨认身份特征",
                    "服装颜色与材质",
                    "表情",
                ],
                "keepFromTemplate": ["撑起夸张斗篷轮廓的服装结构"],
                "reason": "夸张斗篷轮廓构成模板核心玩法",
            }
            return add_unified_rendering_decision(analysis)

        accepted = self.run_case(
            "core-clothing-feature-is-minimal-exception",
            core_clothing_feature,
        )
        self.assertEqual(RULES["resultStates"]["completed"], accepted.state)

        def invisible_core_clothing(analysis: dict) -> dict:
            analysis = core_clothing_feature(analysis)
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"]["clothingVisible"] = False
            return analysis

        contradictory = self.run_case(
            "invisible-clothing-cannot-be-a-fixed-core-feature",
            invisible_core_clothing,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], contradictory.state)

        def keyword_free_clothing_detail(analysis: dict) -> dict:
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            decision = subject["identityInheritanceDecision"]
            decision["clothingVisible"] = False
            decision["inheritFromUpload"].append("西装面料与配色")
            decision["keepFromTemplate"] = ["西装的双排金属纽扣"]
            decision["reason"] = "保持模板美观"
            decision["traitClassifications"].update(
                {
                    "西装面料与配色": CLOTHING_TRAIT_KIND,
                    "西装的双排金属纽扣": CLOTHING_TRAIT_KIND,
                }
            )
            decision["traitClassifications"].pop(
                "蜷卧姿态与前爪搭住软垫的动作"
            )
            return add_unified_rendering_decision(analysis)

        keyword_free = self.run_case(
            "keyword-free-clothing-detail-still-obeys-policy",
            keyword_free_clothing_detail,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], keyword_free.state)

        def generic_template_clothing(analysis: dict) -> dict:
            subject = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"] = {
                "clothingVisible": True,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                    "服装颜色与材质": CLOTHING_TRAIT_KIND,
                    "模板服装": CLOTHING_TRAIT_KIND,
                },
                "inheritFromUpload": ["可辨认身份特征", "服装颜色与材质"],
                "keepFromTemplate": ["模板服装"],
                "reason": "保持模板美观",
            }
            return add_unified_rendering_decision(analysis)

        blocked = self.run_case(
            "generic-template-clothing-is-not-core-play",
            generic_template_clothing,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], blocked.state)
        self.assertFalse((blocked.output_dir / "editable-template-spec.json").exists())

    def test_subject_identity_inheritance_scope_is_required_and_disjoint(self) -> None:
        def remove_decision(analysis: dict) -> dict:
            subject = next(
                slot for slot in analysis["slotCandidates"] if slot["type"] == SUBJECT_TYPE
            )
            subject.pop("identityInheritanceDecision")
            return analysis

        missing = self.run_case("subject-identity-inheritance-missing", remove_decision)
        self.assertEqual(RULES["resultStates"]["blocked"], missing.state)
        self.assertFalse((missing.output_dir / "editable-template-spec.json").exists())

        def overlap_scope(analysis: dict) -> dict:
            subject = next(
                slot for slot in analysis["slotCandidates"] if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"] = {
                "clothingVisible": True,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                    "服装": CLOTHING_TRAIT_KIND,
                },
                "inheritFromUpload": ["可辨认身份特征", "服装"],
                "keepFromTemplate": ["服装"],
                "reason": "伪造重叠范围",
            }
            return analysis

        overlap = self.run_case("subject-identity-inheritance-overlap", overlap_scope)
        self.assertEqual(RULES["resultStates"]["blocked"], overlap.state)
        self.assertFalse((overlap.output_dir / "editable-template-spec.json").exists())

        def generic_only_scope(analysis: dict) -> dict:
            subject = next(
                slot for slot in analysis["slotCandidates"] if slot["type"] == SUBJECT_TYPE
            )
            subject["identityInheritanceDecision"] = {
                "clothingVisible": False,
                "traitClassifications": {
                    "可辨认身份特征": IDENTITY_TRAIT_KIND,
                },
                "inheritFromUpload": ["可辨认身份特征"],
                "keepFromTemplate": [],
                "reason": "默认继承用户上传图",
            }
            return analysis

        generic_only = self.run_case(
            "subject-identity-inheritance-generic-only", generic_only_scope
        )
        self.assertEqual(RULES["resultStates"]["blocked"], generic_only.state)
        self.assertFalse(
            (generic_only.output_dir / "editable-template-spec.json").exists()
        )

    def test_subject_presence_and_kind_discriminators_are_required(self) -> None:
        for field in ("hasPrimarySubject", "subjectKind"):
            with self.subTest(field=field):
                def missing_discriminator(analysis: dict, field: str = field) -> dict:
                    analysis.pop(field, None)
                    return analysis

                result = self.run_case(f"missing-{field.lower()}", missing_discriminator)

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

        def explicit_non_person(analysis: dict) -> dict:
            analysis["subjectKind"] = NON_PERSON_KIND
            return analysis

        completed = self.run_case("explicit-non-person-kind", explicit_non_person)
        self.assertEqual(RULES["resultStates"]["completed"], completed.state)

    def test_slot_ids_must_be_unique(self) -> None:
        def duplicate_slot_id(analysis: dict) -> dict:
            duplicate_id = analysis["slotCandidates"][0]["id"]
            analysis["slotCandidates"][1]["id"] = duplicate_id
            analysis["promptTemplate"] = analysis["promptTemplate"].replace("cushion_look", duplicate_id)
            return analysis

        result = self.run_case("duplicate-slot-id", duplicate_slot_id)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_slot_id_schema_is_enforced_before_editable_sidecar(self) -> None:
        def overlong_slot_id(analysis: dict) -> dict:
            old_id = analysis["slotCandidates"][0]["id"]
            long_id = "a" * 41
            analysis["slotCandidates"][0]["id"] = long_id
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(old_id, long_id)
            return analysis

        result = self.run_case("overlong-slot-id", overlong_slot_id)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_prompt_inline_defaults_must_match_slot_defaults(self) -> None:
        def mismatched_default(analysis: dict) -> dict:
            analysis["slotCandidates"][1]["defaultValue"] = "奶白软垫"
            return analysis

        result = self.run_case("mismatched-inline-slot-default", mismatched_default)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_exact_visible_text_exception_binds_value_and_approved_image_sha(self) -> None:
        long_visible_text = "今天先休息一下明天继续努力呀"

        def fake_exact_text(analysis: dict) -> dict:
            slot = analysis["slotCandidates"][1]
            old_default = slot["defaultValue"]
            slot["defaultValue"] = long_visible_text
            slot["type"] = VISIBLE_TEXT_SLOT_TYPE
            slot["semanticRole"] = SLOT_CONTRACT["semanticRoles"]["highValueTextSpan"]
            slot["exactVisibleText"] = True
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(old_default, long_visible_text)
            analysis["defaultValuePreferenceExceptionEvidence"] = {
                slot["id"]: {"reviewed": True, "reason": "精确保留画内可见文字"}
            }
            return analysis

        blocked = self.run_case("unbound-exact-visible-text", fake_exact_text)

        self.assertEqual(RULES["resultStates"]["blocked"], blocked.state)
        self.assertFalse((blocked.output_dir / "editable-template-spec.json").exists())

        def bound_exact_text(analysis: dict) -> dict:
            analysis = fake_exact_text(analysis)
            slot = analysis["slotCandidates"][1]
            slot[TEXT_CONTRACT["slotBindingField"]] = "long-visible-text-region"
            slot["exactVisibleTextEvidence"] = {
                "approvedImageSha256": analysis["visualFactSourceSha256"],
                "visibleText": long_visible_text,
                "evidence": "Approved Template Image 的软垫区域逐字可见",
            }
            analysis[TEXT_ANALYSIS_FIELDS["regions"]] = [
                {
                    TEXT_REGION_FIELDS["identity"]: "long-visible-text-region",
                    TEXT_REGION_FIELDS["sourceText"]: long_visible_text,
                    TEXT_REGION_FIELDS["role"]: TEXT_ROLES["content"],
                    TEXT_REGION_FIELDS["valueClass"]: TEXT_VALUE_CLASSES["primaryVisual"],
                    TEXT_REGION_FIELDS["action"]: TEXT_ACTIONS["openSlot"],
                    TEXT_REGION_FIELDS["slotIdentity"]: slot["id"],
                    TEXT_REGION_FIELDS["selectedText"]: long_visible_text,
                    TEXT_REGION_FIELDS["exactTextEvidence"]: {
                        TEXT_EVIDENCE_FIELDS["language"]: TEXT_LANGUAGES["simplifiedChinese"],
                        TEXT_EVIDENCE_FIELDS["tokens"]: [long_visible_text],
                        TEXT_EVIDENCE_FIELDS["lines"]: [long_visible_text],
                        TEXT_EVIDENCE_FIELDS["caseSensitiveTokens"]: [],
                        TEXT_EVIDENCE_FIELDS["rareSymbols"]: [],
                        TEXT_EVIDENCE_FIELDS["symbolTopology"]: "单行主要视觉文字",
                        TEXT_EVIDENCE_FIELDS["explanation"]: "逐字核对模板图的主要文字区域",
                    },
                }
            ]
            analysis[TEXT_ANALYSIS_FIELDS["inventory"]] = {
                TEXT_INVENTORY_FIELDS["complete"]: True,
                TEXT_INVENTORY_FIELDS["regionIdentities"]: ["long-visible-text-region"],
                TEXT_INVENTORY_FIELDS["explanation"]: "全画布检查后仅有这一组主要文字",
            }
            return analysis

        completed = self.run_case("bound-exact-visible-text", bound_exact_text)
        self.assertEqual(RULES["resultStates"]["completed"], completed.state)

    def test_slot_value_gates_must_contain_all_named_boolean_roles(self) -> None:
        def incomplete_gates(analysis: dict) -> dict:
            analysis["slotCandidates"][0]["valueGates"] = {
                VALUE_GATE_ROLES["userDemand"]: True,
            }
            return analysis

        adapters = ApprovedAnalysisAdapters(incomplete_gates)
        result = run_production(
            {**self.request, "productionItemId": "incomplete-value-gates"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertNotEqual(RULES["resultStates"]["completed"], result.state)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_asset_unit_analysis_is_required_and_control_count_matches_slots(self) -> None:
        def missing_asset_units(analysis: dict) -> dict:
            analysis.pop("assetUnitAnalysis", None)
            return analysis

        missing = self.run_case("missing-asset-unit-analysis", missing_asset_units)

        self.assertEqual(RULES["resultStates"]["blocked"], missing.state)
        self.assertFalse((missing.output_dir / "editable-template-spec.json").exists())

        def wrong_control_count(analysis: dict) -> dict:
            analysis["assetUnitAnalysis"][ASSET_COUNT_FIELDS["controls"]] = 99
            return analysis

        wrong = self.run_case("wrong-control-unit-count", wrong_control_count)

        self.assertEqual(RULES["resultStates"]["blocked"], wrong.state)
        self.assertFalse((wrong.output_dir / "editable-template-spec.json").exists())

    def test_secondary_text_stays_free_editable_and_asset_units_are_counted_independently(self) -> None:
        secondary_text = "墙上小字写着慢慢来"

        def analyze_units(analysis: dict) -> dict:
            analysis["promptTemplate"] = analysis["promptTemplate"].removesuffix("。") + f"，{secondary_text}。"
            analysis["freeEditableContent"].append(secondary_text)
            analysis["assetUnitAnalysis"] = {
                ASSET_COUNT_FIELDS["visibleSubjects"]: 1,
                ASSET_COUNT_FIELDS["identities"]: 1,
                ASSET_COUNT_FIELDS["uploads"]: 1,
                ASSET_COUNT_FIELDS["controls"]: 3,
                "evidence": "单个可见身份使用一张上传素材，控件按三个高价值编辑入口计算",
            }
            return analysis

        result = self.run_case("secondary-text-and-independent-asset-units", analyze_units)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        record = load_json(result.gallery_template)
        self.assertIn(secondary_text, editable["freeEditableContent"])
        self.assertIn(secondary_text, editable["promptTemplate"])
        self.assertNotIn(secondary_text, json.dumps(record["inputSchema"], ensure_ascii=False))
        self.assertEqual(1, editable["assetUnitAnalysis"][ASSET_COUNT_FIELDS["visibleSubjects"]])
        self.assertEqual(1, editable["assetUnitAnalysis"][ASSET_COUNT_FIELDS["identities"]])
        self.assertEqual(1, editable["assetUnitAnalysis"][ASSET_COUNT_FIELDS["uploads"]])
        self.assertEqual(3, editable["assetUnitAnalysis"][ASSET_COUNT_FIELDS["controls"]])

    def test_semantic_audit_requires_structured_evidence(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_audit = adapters.audit_semantics

        def evidence_free_audit(content: dict) -> dict:
            audit = original_audit(content)
            audit.pop("evidence", None)
            return audit

        adapters.audit_semantics = evidence_free_audit
        result = run_production(
            {**self.request, "productionItemId": "semantic-audit-without-evidence"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

        scalar_adapters = DeterministicFixtureAdapters(FIXTURE)
        original_scalar_audit = scalar_adapters.audit_semantics

        def scalar_evidence_audit(content: dict) -> dict:
            audit = original_scalar_audit(content)
            audit["evidence"] = {
                contract["evidence"]: "ok"
                for contract in RULES["semanticAuditChecks"].values()
            }
            return audit

        scalar_adapters.audit_semantics = scalar_evidence_audit
        scalar = run_production(
            {**self.request, "productionItemId": "semantic-audit-with-scalar-evidence"},
            self.output_root,
            scalar_adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], scalar.state)
        self.assertEqual([], scalar_adapters.upload_calls)

        incomplete_adapters = DeterministicFixtureAdapters(FIXTURE)
        original_incomplete_audit = incomplete_adapters.audit_semantics
        resolved_cases_field = RULES["semanticAuditChecks"]["resolvedPrompts"]["evidence"]

        def incomplete_coverage_audit(content: dict) -> dict:
            audit = original_incomplete_audit(content)
            audit["evidence"][resolved_cases_field] = ["defaults"]
            return audit

        incomplete_adapters.audit_semantics = incomplete_coverage_audit
        incomplete = run_production(
            {**self.request, "productionItemId": "semantic-audit-incomplete-coverage"},
            self.output_root,
            incomplete_adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], incomplete.state)
        self.assertEqual([], incomplete_adapters.upload_calls)

    def test_instruction_scope_and_hidden_layer_responsibilities_are_enforced(self) -> None:
        def out_of_scope_instruction(analysis: dict) -> dict:
            analysis["renderingCoherenceDecision"]["medium"] = (
                "媒介层同时固定暖黄色软垫"
            )
            return analysis

        out_of_scope = self.run_case("out-of-scope-instruction", out_of_scope_instruction)
        self.assertEqual(RULES["resultStates"]["blocked"], out_of_scope.state)

        def duplicated_hidden_responsibility(analysis: dict) -> dict:
            analysis["runtimeSemantics"]["visualContract"]["relations"].append(
                analysis["slotCandidates"][1]["defaultValue"]
            )
            return analysis

        duplicated = self.run_case("duplicated-hidden-responsibility", duplicated_hidden_responsibility)
        self.assertEqual(RULES["resultStates"]["blocked"], duplicated.state)

    def test_subject_input_copy_accepts_optional_author_wording(self) -> None:
        def custom_subject_copy(analysis: dict) -> dict:
            subject = analysis["slotCandidates"][0]
            subject[SUBJECT_PROMPT_VALUE_FIELD] = "用户上传图中的蜷卧小动物"
            subject[SUBJECT_HINT_FIELD] = "上传1张单只小动物清晰照片，用于替换软垫上蜷卧的主体"
            return analysis

        result = self.run_case("custom-subject-input-copy", custom_subject_copy)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        formal = load_json(result.gallery_template)
        subject = next(item for item in formal["inputSchema"]["slots"] if item["id"] == "pet_subject")
        self.assertEqual("用户上传图中的蜷卧小动物", subject["image"]["promptValue"])
        self.assertEqual(
            "上传1张单只小动物清晰照片，用于替换软垫上蜷卧的主体",
            subject["image"]["hint"],
        )
        subject_text = next(
            value
            for value in subject.values()
            if isinstance(value, dict) and "allowCustom" in value
        )
        self.assertEqual("描述想替换的小动物", subject_text["placeholder"])

    def test_subject_input_uses_contract_defaults_when_optional_copy_is_absent(self) -> None:
        def subject_without_optional_copy(analysis: dict) -> dict:
            subject = analysis["slotCandidates"][0]
            subject.pop(SUBJECT_PROMPT_VALUE_FIELD, None)
            subject.pop(SUBJECT_HINT_FIELD, None)
            return analysis

        result = self.run_case(
            "subject-input-with-contract-defaults", subject_without_optional_copy
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        formal = load_json(result.gallery_template)
        subject = next(item for item in formal["inputSchema"]["slots"] if item["id"] == "pet_subject")
        self.assertEqual(
            SUBJECT_DEFAULTS["imagePromptValue"], subject["image"]["promptValue"]
        )
        self.assertEqual(SUBJECT_DEFAULTS["imageHint"], subject["image"]["hint"])

    def test_visual_contract_rejects_generic_reference_only_style_language(self) -> None:
        for index, forbidden_fragment in enumerate(FORBIDDEN_VISUAL_REFERENCE_FRAGMENTS):
            with self.subTest(fragment=forbidden_fragment):
                def generic_style_contract(analysis: dict) -> dict:
                    analysis["renderingCoherenceDecision"]["medium"] = (
                        forbidden_fragment
                    )
                    return analysis

                result = self.run_case(
                    f"generic-reference-only-style-{index}", generic_style_contract
                )

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertFalse((result.output_dir / "gallery-template.json").exists())

        def vague_visual_contract(analysis: dict) -> dict:
            analysis["runtimeSemantics"]["visualContract"] = {
                "medium": "高质量插画",
                "styleTraits": ["精美细节"],
                "composition": ["合理构图"],
                "relations": ["自然关系"],
                "colorAndLight": [],
            }
            return analysis

        vague = self.run_case("vague-visual-contract", vague_visual_contract)
        self.assertEqual(RULES["resultStates"]["blocked"], vague.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], vague.error_code)
        self.assertFalse((vague.output_dir / "gallery-template.json").exists())

    def test_rendering_decision_is_the_authority_for_formal_medium_and_style(self) -> None:
        expected_medium = "统一的二维数字平涂插画，所有人物、手部和物件使用同一闭合线稿体系"
        expected_traits = [
            "人物、手部与物件统一使用清晰闭合外轮廓、低渐变色块和少量平面高光"
        ]

        def decide_rendering(analysis: dict) -> dict:
            analysis["runtimeSemantics"]["visualContract"]["medium"] = (
                "冲突的三维写实产品摄影媒介"
            )
            analysis["runtimeSemantics"]["visualContract"]["styleTraits"] = [
                "冲突的真实皮肤纹理、摄影景深与塑料玩具材质"
            ]
            return add_unified_rendering_decision(
                analysis,
                medium=expected_medium,
                style_traits=expected_traits,
            )

        result = run_production(
            {
                **self.request,
                "productionItemId": "rendering-decision-authority",
            },
            self.output_root,
            PostRebuildApprovedAnalysisAdapters(decide_rendering),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        formal = load_json(result.gallery_template)
        self.assertEqual(
            expected_medium,
            formal["runtimeSemantics"]["visualContract"]["medium"],
        )
        self.assertEqual(
            expected_traits,
            formal["runtimeSemantics"]["visualContract"]["styleTraits"],
        )
        self.assertEqual(
            expected_medium,
            editable["renderingCoherenceDecision"]["medium"],
        )

    def test_rendering_decision_must_cover_every_approved_component(self) -> None:
        def omit_holding_component(analysis: dict) -> dict:
            analysis = add_unified_rendering_decision(analysis)
            unit = analysis["renderingCoherenceDecision"]["renderingUnits"][0]
            unit["componentIds"].remove(unit["componentIds"][-2])
            return analysis

        adapters = PostRebuildApprovedAnalysisAdapters(omit_holding_component)
        result = run_production(
            {**self.request, "productionItemId": "rendering-component-omitted"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_subject_transfer_requires_complete_redraw_and_exact_authority(self) -> None:
        def incomplete_redraw(analysis: dict) -> dict:
            analysis = add_unified_rendering_decision(analysis)
            transfer = analysis["renderingCoherenceDecision"]["subjectTransfers"][0]
            transfer["completeRedraw"] = False
            transfer["keepFromTemplate"] = ["默认服装"]
            return analysis

        adapters = PostRebuildApprovedAnalysisAdapters(incomplete_redraw)
        result = run_production(
            {**self.request, "productionItemId": "subject-transfer-incomplete"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_intentional_mixed_rendering_requires_explicit_boundary_evidence(self) -> None:
        def mixed_decision(analysis: dict, *, with_boundary: bool) -> dict:
            analysis = add_unified_rendering_decision(analysis)
            decision = analysis["renderingCoherenceDecision"]
            all_components = decision["renderingUnits"][0]["componentIds"]
            subject_target = decision["subjectTransfers"][0]["targetIds"][0]
            subject_components = [subject_target]
            environment_components = [
                component_id
                for component_id in all_components
                if component_id != subject_target
            ]
            decision["mode"] = "intentional_mixed"
            decision["renderingUnits"] = [
                {
                    "unitId": "illustrated-subject",
                    "componentIds": subject_components,
                    "styleTraits": ["主体使用闭合线稿和低渐变二维平涂色块"],
                    "evidence": "确认图中主体轮廓明确采用二维插画处理",
                },
                {
                    "unitId": "textured-environment",
                    "componentIds": environment_components,
                    "styleTraits": ["环境保留颗粒化织物与柔和景深纹理"],
                    "evidence": "确认图中承托物和背景有独立材质纹理",
                },
            ]
            decision["subjectTransfers"][0]["renderingUnitId"] = (
                "illustrated-subject"
            )
            decision["boundaryEvidence"] = (
                ["主体闭合轮廓与环境材质在接触边界清楚分层，属于设计事实"]
                if with_boundary
                else []
            )
            return analysis

        valid = run_production(
            {**self.request, "productionItemId": "intentional-mixed-valid"},
            self.output_root,
            PostRebuildApprovedAnalysisAdapters(
                lambda analysis: mixed_decision(analysis, with_boundary=True)
            ),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["completed"], valid.state)

        adapters = PostRebuildApprovedAnalysisAdapters(
            lambda analysis: mixed_decision(analysis, with_boundary=False)
        )
        invalid = run_production(
            {**self.request, "productionItemId": "intentional-mixed-no-boundary"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual(RULES["resultStates"]["blocked"], invalid.state)
        self.assertEqual([], adapters.upload_calls)

    def test_rendering_coherence_decision_is_required_before_delivery(self) -> None:
        def remove_rendering_decision(analysis: dict) -> dict:
            analysis.pop("renderingCoherenceDecision")
            return analysis

        adapters = ApprovedAnalysisAdapters(remove_rendering_decision)
        result = run_production(
            {**self.request, "productionItemId": "rendering-decision-missing"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_p6_blocks_visual_contract_that_mismatches_the_approved_image(self) -> None:
        class MediumMismatchAdapters(ApprovedAnalysisAdapters):
            def audit_visual_contract(
                self, approved_image: Path, review_request: dict
            ) -> dict:
                self.visual_contract_audit_calls = (
                    getattr(self, "visual_contract_audit_calls", 0) + 1
                )
                visual_contract = review_request["visualContract"]
                visual_contract_sha = hashlib.sha256(
                    json.dumps(
                        visual_contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                decision = review_request["renderingCoherenceDecision"]
                decision_sha = hashlib.sha256(
                    json.dumps(
                        decision,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                return {
                    "approvedImageSha256": hashlib.sha256(
                        approved_image.read_bytes()
                    ).hexdigest(),
                    "visualContractSha256": visual_contract_sha,
                    "renderingCoherenceDecisionSha256": decision_sha,
                    "mediumMatchesApprovedImage": False,
                    "compositionMatchesApprovedImage": True,
                    "relationsMatchApprovedImage": True,
                    "renderingUnitReviews": [
                        {
                            "unitId": unit["unitId"],
                            "componentIds": unit["componentIds"],
                            "matchesApprovedImage": True,
                            "evidence": "逐组件检查渲染单元",
                        }
                        for unit in decision["renderingUnits"]
                    ],
                    "subjectTransferReviews": [
                        {
                            "inputId": transfer["inputId"],
                            "targetIds": transfer["targetIds"],
                            "completeRedraw": True,
                            "authorityMatches": True,
                            "evidence": "逐项检查主体完整转绘与权限",
                        }
                        for transfer in decision["subjectTransfers"]
                    ],
                    "evidence": "确认图是平涂插画，待交付合同却声明三维写实媒介",
                }

        adapters = MediumMismatchAdapters(lambda analysis: analysis)
        result = run_production(
            {
                **self.request,
                "productionItemId": "visual-contract-medium-mismatch",
            },
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual(1, adapters.visual_contract_audit_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_p6_blocks_one_mismatched_rendering_region_before_oss(self) -> None:
        class RegionMismatchAdapters(ApprovedAnalysisAdapters):
            def audit_visual_contract(
                self, approved_image: Path, review_request: dict
            ) -> dict:
                review = super().audit_visual_contract(
                    approved_image, review_request
                )
                review["renderingUnitReviews"][0]["matchesApprovedImage"] = False
                review["renderingUnitReviews"][0]["evidence"] = (
                    "手部仍是简化写实体积，人物与耳机盒却是二维平涂"
                )
                return review

        adapters = RegionMismatchAdapters(lambda analysis: analysis)
        result = run_production(
            {**self.request, "productionItemId": "rendering-region-mismatch"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_p6_blocks_subject_garment_authority_mismatch_before_oss(self) -> None:
        class GarmentAuthorityMismatchAdapters(ApprovedAnalysisAdapters):
            def audit_visual_contract(
                self, approved_image: Path, review_request: dict
            ) -> dict:
                review = super().audit_visual_contract(
                    approved_image, review_request
                )
                transfer = review["subjectTransferReviews"][0]
                transfer["authorityMatches"] = False
                transfer["evidence"] = "生成规则额外保留默认服装，超出已声明模板权限"
                return review

        adapters = GarmentAuthorityMismatchAdapters(lambda analysis: analysis)
        result = run_production(
            {**self.request, "productionItemId": "garment-authority-mismatch"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_p6_blocks_composition_or_action_facts_that_do_not_match_image(self) -> None:
        cases = {
            "compositionMatchesApprovedImage": "构图遗漏主体裁切、倾斜方向和近景占比",
            "relationsMatchApprovedImage": "动作关系遗漏托举接触与身体朝向",
        }
        for index, (field, evidence) in enumerate(cases.items()):
            with self.subTest(field=field):
                class FactMismatchAdapters(ApprovedAnalysisAdapters):
                    def audit_visual_contract(
                        self, approved_image: Path, review_request: dict
                    ) -> dict:
                        review = super().audit_visual_contract(
                            approved_image, review_request
                        )
                        review[field] = False
                        review["evidence"] = evidence
                        return review

                adapters = FactMismatchAdapters(lambda analysis: analysis)
                result = run_production(
                    {
                        **self.request,
                        "productionItemId": f"visual-fact-mismatch-{index}",
                    },
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)
                self.assertFalse(
                    (result.output_dir / "gallery-template.json").exists()
                )

    def test_v1_prompt_enhancement_cannot_supply_v2_runtime_semantics(self) -> None:
        def legacy_only(analysis: dict) -> dict:
            analysis.pop("runtimeSemantics", None)
            analysis.pop("visualContract", None)
            analysis["promptEnhancement"] = {
                "instruction": "高质量插画",
                "lockedConstraints": ["精美细节"],
                "preserve": ["自然关系"],
            }
            return analysis

        result = self.run_case("legacy-prompt-enhancement-only", legacy_only)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_title_requires_all_current_image_authoring_gates(self) -> None:
        def title_copied_without_grounding(analysis: dict) -> dict:
            analysis["titleEvidence"]["templateGrounded"] = False
            analysis["titleEvidence"]["evidence"] = "标题沿用上一张模板"
            return analysis

        result = self.run_case("title-without-current-image-grounding", title_copied_without_grounding)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_each_item_compiles_targets_and_style_from_its_own_approved_image(self) -> None:
        request = load_json(UNSEEN_FORWARD_FIXTURE / "request.json")
        request["sourceImage"] = str(
            UNSEEN_FORWARD_FIXTURE / request["sourceImage"]
        )

        result = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(UNSEEN_FORWARD_FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        formal = load_json(result.gallery_template)
        current_item_semantics = json.dumps(
            formal["runtimeSemantics"], ensure_ascii=False
        )
        self.assertIn("树根", current_item_semantics)
        self.assertIn("林地", current_item_semantics)
        for sibling_item_fact in ("软垫", "客厅", "蜷卧"):
            self.assertNotIn(sibling_item_fact, current_item_semantics)

    def test_prompt_slots_require_nonempty_suggestion_pools(self) -> None:
        def empty_prompt_suggestions(analysis: dict) -> dict:
            analysis["slotCandidates"][1]["suggestions"] = []
            return analysis

        result = self.run_case("empty-prompt-suggestion-pool", empty_prompt_suggestions)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_suggestion_duplicates_are_compared_as_trimmed_user_visible_values(self) -> None:
        def trimmed_duplicate(analysis: dict) -> dict:
            first = analysis["slotCandidates"][1]["suggestions"][0]
            analysis["slotCandidates"][1]["suggestions"].append(f" {first} ")
            return analysis

        duplicate = self.run_case("trimmed-duplicate-suggestion", trimmed_duplicate)
        self.assertEqual(RULES["resultStates"]["blocked"], duplicate.state)

        def trimmed_default_duplicate(analysis: dict) -> dict:
            slot = analysis["slotCandidates"][1]
            slot["suggestions"][0] = f" {slot['defaultValue']} "
            return analysis

        default_duplicate = self.run_case("trimmed-default-duplicate", trimmed_default_duplicate)
        self.assertEqual(RULES["resultStates"]["blocked"], default_duplicate.state)

    def test_semantic_adapter_cannot_mutate_compiled_core_objects(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_audit = adapters.audit_semantics

        def mutating_audit(content: dict) -> dict:
            first = content["slots"][1]["suggestions"][0]
            content["slots"][1]["suggestions"].append(f" {first} ")
            content["runtimeSemantics"]["visualContract"]["relations"].append(
                "审计期间注入的语义锚点"
            )
            audit = original_audit(content)
            digest = hashlib.sha256(
                json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            audit["contentSha256"] = digest
            audit["observedContentSha256"] = digest
            return audit

        adapters.audit_semantics = mutating_audit
        result = run_production(
            {**self.request, "productionItemId": "mutating-semantic-adapter"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        draft = load_json(result.output_dir / "gallery-template.draft.json")
        self.assertEqual(3, len(editable["slots"][1]["suggestions"]))
        self.assertNotIn(
            "审计期间注入的语义锚点",
            draft["runtimeSemantics"]["visualContract"]["relations"],
        )
        self.assertFalse((result.output_dir / "gallery-template.json").exists())
        self.assertEqual([], adapters.upload_calls)


if __name__ == "__main__":
    unittest.main()
