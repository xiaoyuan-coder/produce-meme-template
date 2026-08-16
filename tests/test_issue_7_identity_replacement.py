from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Callable

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
IDENTITY_FIXTURE = ROOT / "fixtures" / "e2e" / "identity-routes"
RULES = json.loads((ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8"))
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")
SLOT_CONTRACT = RULES["slotCompilationContract"]
VALUE_GATES = SLOT_CONTRACT["valueGateRoles"]
PERSON_ATTRIBUTES = SLOT_CONTRACT["personAttributeRoles"]
SUBJECT_ROLE = SLOT_CONTRACT["semanticRoles"]["primarySubject"]
PERSON_KIND = SLOT_CONTRACT["subjectKinds"]["humanSubject"]
COUNT_FIELDS = SLOT_CONTRACT["assetUnitCountFields"]
IDENTITY_CONTRACT = RULES["identityReplacementContract"]
IDENTITY_PLAN_FIELDS = IDENTITY_CONTRACT["planFields"]
IDENTITY_SOURCE_FIELDS = IDENTITY_CONTRACT["sourceFields"]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


SCENARIOS = load_json(IDENTITY_FIXTURE / "scenarios.json")


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def candidate_card(prefix: str) -> dict:
    fields = IDENTITY_CONTRACT["candidateCardFields"]
    return {
        fields["anchors"]: [f"{prefix}的头部轮廓", f"{prefix}的标志性配色"],
        fields["antiAnchors"]: ["不沿用旧身份脸部特征", "不混入其他角色标识"],
        fields["playFusion"]: ["保持来源姿态与画面职责", "保持原媒介与构图骨架"],
    }


class IdentityScenarioAdapters(DeterministicFixtureAdapters):
    def __init__(
        self,
        scenario: dict,
        *,
        source_transform: Callable[[dict], dict] | None = None,
        approved_transform: Callable[[dict], dict] | None = None,
        identity_text_applicable: bool = True,
        source_identity_terms_absent: bool = True,
        identity_text_consistent: bool = True,
        identity_neutral_defaults_valid: bool = True,
    ):
        super().__init__(FIXTURE)
        self.scenario = copy.deepcopy(scenario)
        self.source_transform = source_transform
        self.approved_transform = approved_transform
        self.identity_text_applicable = identity_text_applicable
        self.source_identity_terms_absent = source_identity_terms_absent
        self.identity_text_consistent = identity_text_consistent
        self.identity_neutral_defaults_valid = identity_neutral_defaults_valid

    def analyze_source(self, source_image: Path, replacement_strategy: dict | None) -> dict:
        analysis = super().analyze_source(source_image, replacement_strategy)
        scenario = self.scenario
        route = IDENTITY_CONTRACT["routes"][scenario["routeRole"]]
        category = RULES["sourceCategories"][route["sourceCategoryRole"]]
        analysis["target"] = {
            "category": category,
            "role": "画面中央的主要身份人物",
            "identity": scenario["sourceIdentity"],
        }
        selected = {
            "value": scenario["replacementValue"],
            "category": category,
            "semanticCompatible": True,
            "visualCompatible": True,
            "rightsAndSafety": "pass",
            "score": 0.96,
            "reason": "同类身份、姿态可供性和画面玩法相容",
        }
        distinct_field = IDENTITY_CONTRACT["candidateFields"]["distinctIdentityEvidence"]
        distinct_fields = IDENTITY_CONTRACT["distinctIdentityEvidenceFields"]
        selected[distinct_field] = {
            distinct_fields["sourceIdentity"]: scenario["sourceIdentity"],
            distinct_fields["candidateIdentity"]: scenario["replacementValue"],
            distinct_fields["distinct"]: True,
            distinct_fields["explanation"]: "逐项比对身份名称、别名和识别锚点后确认是新身份",
        }
        if route["candidateCardRequired"]:
            selected[IDENTITY_CONTRACT["candidateFields"]["card"]] = candidate_card(
                scenario["replacementValue"]
            )
        analysis["replacementPool"] = [selected]
        dependency_fields = IDENTITY_CONTRACT["dependencyFields"]
        component_field = dependency_fields["componentIdentity"]
        type_field = dependency_fields["dependencyType"]
        value_field = dependency_fields["description"]
        dependency_types = IDENTITY_CONTRACT["dependencyTypes"]
        closure = [
            {
                component_field: f"identity-component-{index}",
                type_field: dependency_types[dependency_role],
                value_field: (
                    f"{scenario['sourceIdentity']}的可见身份文字"
                    if dependency_role == "identityText"
                    else f"{scenario['sourceIdentity']}的{dependency_types[dependency_role]}区域"
                ),
            }
            for index, dependency_role in enumerate(scenario["dependencyTypeRoles"])
        ]
        text_component = next(
            item
            for item in closure
            if item[type_field] == dependency_types["identityText"]
        )
        analysis["dependencyClosure"] = closure
        topology_fields = IDENTITY_CONTRACT["topologyFields"]
        analysis[IDENTITY_SOURCE_FIELDS["topology"]] = {
            topology_fields["requiredComponents"]: [item[component_field] for item in closure],
            topology_fields["identityTextComponents"]: [text_component[component_field]],
            topology_fields["explanation"]: "逐区域检查主体、重复实例、派生区域与身份文字",
        }
        decision_fields = IDENTITY_CONTRACT["identityTextDecisionFields"]
        decision = {
            decision_fields["componentIdentity"]: text_component[component_field],
            decision_fields["sourceText"]: scenario["sourceIdentity"],
            decision_fields["action"]: IDENTITY_CONTRACT["identityTextActions"][
                scenario["identityTextActionRole"]
            ],
            decision_fields["result"]: scenario["identityTextResult"],
            decision_fields["basis"]: "根据主体是否继续开放与文字的独立编辑价值决定",
        }
        if scenario["exposeText"]:
            decision[decision_fields["highValueEvidence"]] = "画面标题栏是可见主要文字，且有独立定制动机"
        if scenario["identityTextRelationshipRole"] is not None:
            decision[decision_fields["relationshipType"]] = IDENTITY_CONTRACT[
                "identityTextRelationshipTypes"
            ][scenario["identityTextRelationshipRole"]]
            decision[decision_fields["replacementIdentity"]] = scenario["replacementValue"]
        analysis[IDENTITY_SOURCE_FIELDS["textDecisions"]] = [decision]
        route_fields = IDENTITY_CONTRACT["routeEvidenceFields"]
        analysis[IDENTITY_SOURCE_FIELDS["routeEvidence"]] = {
            route_fields["mode"]: route["mode"],
            route_fields["localAssetRequirement"]: route["localAssetRequired"],
            route_fields["completeRedraw"]: True,
            route_fields["explanation"]: "完整人物与身份依赖统一重绘，身份参考资产只是可选质量辅助",
        }
        analysis["forbiddenLegacyClaims"] = [scenario["sourceIdentity"]]
        analysis["frozenSet"] = ["构图骨架", "人物的画面职责", "原有媒介"]
        frozen_fields = IDENTITY_CONTRACT["frozenConflictEvaluationFields"]
        analysis[IDENTITY_SOURCE_FIELDS["frozenConflictEvaluations"]] = [
            {
                frozen_fields["frozenValue"]: value,
                frozen_fields["conflict"]: False,
                frozen_fields["componentIdentities"]: [],
                frozen_fields["explanation"]: "逐项对照身份拓扑后未发现与身份重绘重叠",
            }
            for value in analysis["frozenSet"]
        ]
        if self.source_transform:
            analysis = self.source_transform(analysis)
        return analysis

    def inspect_generated(self, generated_image: Path, review_context: dict[str, str]) -> dict:
        review = super().inspect_generated(generated_image, review_context)
        contract = RULES["visualReviewContract"]
        evidence_field = contract["evidenceFieldRoles"]["identityText"]
        evidence_fields = contract["identityTextEvidenceFields"]
        review[evidence_field] = {
            evidence_fields["applicability"]: self.identity_text_applicable,
            evidence_fields["legacyTermsAbsent"]: self.source_identity_terms_absent,
            evidence_fields["replacementConsistency"]: self.identity_text_consistent,
            evidence_fields["explanation"]: "全画布核对了旧身份与新身份文字的删除、中性化或同步结果",
        }
        evidence_payload = {
            field: review[field]
            for field in contract["evidenceFieldRoles"].values()
        }
        review["bindings"]["evidenceSha256"] = canonical_sha(evidence_payload)
        return review

    def analyze_approved(self, approved_image: Path) -> dict:
        analysis = super().analyze_approved(approved_image)
        scenario = self.scenario
        subject = analysis["slotCandidates"][0]
        subject.update(
            {
                "id": "portrait_subject",
                "type": "subject",
                "semanticRole": SUBJECT_ROLE,
                "label": "画面人物",
                "placeholder": "描述想替换的人物或角色",
                "defaultValue": scenario["subjectDefault"],
                "suggestions": scenario["subjectSuggestions"],
                "hiddenConflictTokens": ["主体身份", "具体人物", "具体角色"],
                "titleForbiddenTokens": [
                    scenario["sourceIdentity"],
                    scenario["replacementValue"],
                    *scenario["subjectSuggestions"],
                ],
            }
        )
        analysis["neutralTitle"] = "多重光影里的主角时刻"
        analysis["neutralDescription"] = "中央人物与重复剪影形成层次，光影和边框保留戏剧感"
        analysis["promptTemplate"] = (
            f'一位{{{{ portrait_subject | "{scenario["subjectDefault"]}" }}}}站在画面中央，'
            '{{ cushion_look | "暖黄色软垫" }}作为前景承托，'
            '{{ room_mood | "午后窗光" }}勾勒主体、剪影和边框层次。'
        )
        analysis["freeEditableContent"] = ["站在画面中央", "剪影和边框层次"]
        analysis["subjectKind"] = PERSON_KIND
        analysis["subjectAttributeAssessments"] = {
            role: {
                **{gate: False for gate in VALUE_GATES.values()},
                "includedAsSlot": False,
                "evidence": f"已独立评估 {role}，当前画面不具备独立开放价值",
            }
            for role in PERSON_ATTRIBUTES.values()
        }
        analysis["assetUnitAnalysis"].update(
            {
                COUNT_FIELDS["visibleSubjects"]: 1,
                COUNT_FIELDS["identities"]: 1,
                COUNT_FIELDS["uploads"]: 1,
                COUNT_FIELDS["controls"]: 3,
                "evidence": "单身份在画内含重复派生实例，仍只需一份身份上传素材",
            }
        )
        analysis["promptEnhancement"] = {
            "instruction": "媒介：保持画面的统一摄影或插画质感。卖点：中央主角、剪影与边框形成层次。色彩：可随用户调整前景与环境光。",
            "lockedConstraints": ["保持画幅、主体占比与重复实例的层级", "保持前后遮挡和接触边界"],
            "preserve": ["保持主角与派生实例的统一叙事职责"],
        }
        analysis["tags"] = ["人物", "光影", "多重实例"]
        if scenario["omitSubject"]:
            analysis["slotCandidates"] = analysis["slotCandidates"][1:]
            analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                f'{{{{ portrait_subject | "{scenario["subjectDefault"]}" }}}}',
                "画面主人物",
            )
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    gate: gate != VALUE_GATES["userDemand"]
                    for gate in VALUE_GATES.values()
                },
                "reason": "当前公众人物与已同步身份文字共同构成固定玩法，本次不开放主体",
            }
            analysis["assetUnitAnalysis"][COUNT_FIELDS["uploads"]] = 0
            analysis["assetUnitAnalysis"][COUNT_FIELDS["controls"]] = 2
        if scenario["exposeText"]:
            text_slot = {
                "id": "identity_label",
                "type": "text",
                "semanticRole": SLOT_CONTRACT["semanticRoles"]["identityText"],
                "label": "画面标签",
                "placeholder": "输入中性人物标签",
                "defaultValue": scenario["identityTextResult"],
                "suggestions": ["YOUR NAME", "PROFILE", "HERO"],
                "hiddenConflictTokens": ["身份文字", "人物姓名", "角色名"],
                "titleForbiddenTokens": ["PORTRAIT", "YOUR NAME", "PROFILE", "HERO"],
                "valueGates": {gate: True for gate in VALUE_GATES.values()},
                "exactVisibleText": True,
                "exactVisibleTextEvidence": {
                    "approvedImageSha256": analysis["visualFactSourceSha256"],
                    "visibleText": scenario["identityTextResult"],
                    "evidence": "标题栏中的中性大字清晰可辨",
                },
            }
            analysis["slotCandidates"].append(text_slot)
            analysis["promptTemplate"] = analysis["promptTemplate"].removesuffix("。") + (
                f'，标题栏写着{{{{ identity_label | "{scenario["identityTextResult"]}" }}}}。'
            )
            analysis["assetUnitAnalysis"][COUNT_FIELDS["controls"]] = 4
            analysis["defaultValuePreferenceExceptionEvidence"] = {
                "identity_label": {
                    "reviewed": True,
                    "reason": "当前确认模板图使用简短英文中性标签",
                }
            }
        if self.approved_transform:
            analysis = self.approved_transform(analysis)
        return analysis

    def audit_semantics(self, content: dict) -> dict:
        audit = super().audit_semantics(content)
        audit_contract = RULES["semanticAuditChecks"]["identityNeutrality"]
        audit_fields = IDENTITY_CONTRACT["neutralityAuditFields"]
        subject_upload_type = SLOT_CONTRACT["slotTypes"]["primarySubjectUpload"]
        neutrality_applicable = any(
            slot["type"] == subject_upload_type for slot in content["slots"]
        )
        audit["checks"][audit_contract["check"]] = self.identity_neutral_defaults_valid
        audit["evidence"][audit_contract["evidence"]] = {
            audit_fields["applicability"]: neutrality_applicable,
            audit_fields["specificIdentityDetected"]: not self.identity_neutral_defaults_valid,
            audit_fields["explanation"]: (
                "逐项核对标题、描述、固定 Prompt、隐藏层与全部非主体槽文案"
            ),
        }
        digest = canonical_sha(content)
        audit["contentSha256"] = digest
        audit["observedContentSha256"] = digest
        return audit


class Issue7IdentityReplacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_case(self, item_id: str, adapters: IdentityScenarioAdapters):
        return run_production(
            {**self.request, "productionItemId": item_id},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

    def test_ordinary_public_and_known_ip_routes_complete_end_to_end(self) -> None:
        observed_dependency_types = set()

        for name, scenario in SCENARIOS.items():
            with self.subTest(route=name):
                result = self.run_case(f"identity-route-{name}", IdentityScenarioAdapters(scenario))
                self.assertEqual(RULES["resultStates"]["completed"], result.state)
                plan = load_json(result.output_dir / "replacement-plan.json")
                package = load_json(result.output_dir / "generation-package.json")
                editable = load_json(result.output_dir / "editable-template-spec.json")
                formal = load_json(result.gallery_template)
                target = plan["primaryTargets"][0]
                route = IDENTITY_CONTRACT["routes"][scenario["routeRole"]]
                route_fields = IDENTITY_CONTRACT["routeEvidenceFields"]
                decision_fields = IDENTITY_CONTRACT["identityTextDecisionFields"]
                route_plan = plan[IDENTITY_PLAN_FIELDS["route"]]
                text_decisions = plan[IDENTITY_PLAN_FIELDS["textDecisions"]]
                section_roles = IDENTITY_CONTRACT["generationSectionRoles"]
                self.assertEqual(route["mode"], route_plan[route_fields["mode"]])
                self.assertEqual(
                    IDENTITY_CONTRACT["identityTextActions"][scenario["identityTextActionRole"]],
                    text_decisions[0][decision_fields["action"]],
                )
                self.assertIn(section_roles["route"], package["sections"])
                self.assertIn(section_roles["identityText"], package["sections"])
                self.assertEqual(set(PERSON_ATTRIBUTES.values()), set(editable["subjectAttributeAssessments"]))
                self.assertTrue(
                    all(
                        assessment["includedAsSlot"] is False
                        for assessment in editable["subjectAttributeAssessments"].values()
                    )
                )
                self.assertFalse(
                    set(PERSON_ATTRIBUTES.values())
                    & {slot["semanticRole"] for slot in editable["slots"]}
                )
                dependency_type_field = IDENTITY_CONTRACT["dependencyFields"]["dependencyType"]
                observed_dependency_types.update(
                    item[dependency_type_field] for item in plan["dependencyClosure"]
                )
                serialized = json.dumps(formal, ensure_ascii=False)
                self.assertNotIn(scenario["sourceIdentity"], serialized)
                self.assertNotIn(scenario["sourceIdentity"], formal["title"])
                self.assertNotIn(scenario["replacementValue"], formal["title"])
                if scenario["routeRole"] == "ordinaryPerson":
                    self.assertFalse(route_plan[route_fields["localAssetRequirement"]])
                    self.assertNotIn(IDENTITY_CONTRACT["candidateFields"]["card"], target)
                else:
                    card = target[IDENTITY_CONTRACT["candidateFields"]["card"]]
                    card_fields = IDENTITY_CONTRACT["candidateCardFields"]
                    self.assertTrue(card[card_fields["anchors"]])
                    self.assertTrue(card[card_fields["antiAnchors"]])
                    self.assertTrue(card[card_fields["playFusion"]])
                if scenario["exposeText"]:
                    text_slot = next(slot for slot in editable["slots"] if slot["id"] == "identity_label")
                    self.assertEqual(scenario["identityTextResult"], text_slot["defaultValue"])

        self.assertTrue(
            {
                IDENTITY_CONTRACT["dependencyTypes"][role]
                for role in (
                    "repeatedInstance",
                    "shadow",
                    "reflection",
                    "framedPortrait",
                    "identityBadge",
                    "identityText",
                )
            }
            <= observed_dependency_types
        )

    def test_public_figure_and_known_ip_candidates_require_complete_candidate_cards(self) -> None:
        for name in ("public", "ip"):
            with self.subTest(route=name):
                def remove_anti_anchors(analysis: dict) -> dict:
                    card = analysis["replacementPool"][0][IDENTITY_CONTRACT["candidateFields"]["card"]]
                    card.pop(IDENTITY_CONTRACT["candidateCardFields"]["antiAnchors"])
                    return analysis

                adapters = IdentityScenarioAdapters(
                    SCENARIOS[name],
                    source_transform=remove_anti_anchors,
                )
                result = self.run_case(f"missing-identity-card-{name}", adapters)
                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
                self.assertEqual([], adapters.generate_calls)

    def test_identity_route_requires_a_nonempty_source_identity(self) -> None:
        def erase_source_identity(analysis: dict) -> dict:
            analysis["target"]["identity"] = ""
            distinct_field = IDENTITY_CONTRACT["candidateFields"]["distinctIdentityEvidence"]
            distinct_fields = IDENTITY_CONTRACT["distinctIdentityEvidenceFields"]
            analysis["replacementPool"][0][distinct_field][
                distinct_fields["sourceIdentity"]
            ] = ""
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["public"],
            source_transform=erase_source_identity,
        )
        result = self.run_case("identity-route-without-source-identity", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_identity_routes_cannot_select_the_source_identity_as_the_replacement(self) -> None:
        for name, scenario in SCENARIOS.items():
            self_reference = IDENTITY_CONTRACT["identityEquivalenceModifiers"]["selfReference"]
            equivalent_values = (
                f"  {scenario['sourceIdentity']}  ",
                f"{scenario['sourceIdentity']}！",
                f"{scenario['sourceIdentity']}（{self_reference}）",
                f"{scenario['sourceIdentity']}{self_reference}",
            )
            for index, equivalent_value in enumerate(equivalent_values):
                with self.subTest(route=name, equivalent=equivalent_value):
                    unchanged = copy.deepcopy(scenario)
                    unchanged["replacementValue"] = equivalent_value
                    if unchanged["identityTextActionRole"] == "synchronize":
                        unchanged["identityTextResult"] = equivalent_value
                    adapters = IdentityScenarioAdapters(unchanged)
                    result = self.run_case(f"unchanged-identity-{name}-{index}", adapters)

                    self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                    self.assertEqual(
                        RULES["errorCodes"]["noCompatibleReplacement"], result.error_code
                    )
                    self.assertEqual([], adapters.generate_calls)

    def test_identity_candidate_cannot_normalize_to_an_empty_name(self) -> None:
        self_reference = IDENTITY_CONTRACT["identityEquivalenceModifiers"]["selfReference"]
        for name, scenario in SCENARIOS.items():
            with self.subTest(route=name):
                unnamed = copy.deepcopy(scenario)
                unnamed["replacementValue"] = f"{self_reference}！"
                if unnamed["identityTextActionRole"] == "synchronize":
                    unnamed["identityTextResult"] = unnamed["replacementValue"]
                adapters = IdentityScenarioAdapters(unnamed)
                result = self.run_case(f"empty-normalized-identity-{name}", adapters)

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(
                    RULES["errorCodes"]["noCompatibleReplacement"], result.error_code
                )
                self.assertEqual([], adapters.generate_calls)

    def test_autonomous_frozen_set_cannot_preserve_identity_content_being_replaced(self) -> None:
        conflicting_value = "保留周杰伦姓名与身份徽标"

        def add_identity_conflict(analysis: dict) -> dict:
            analysis["frozenSet"].append(conflicting_value)
            topology_fields = IDENTITY_CONTRACT["topologyFields"]
            component_ids = analysis[IDENTITY_SOURCE_FIELDS["topology"]][
                topology_fields["identityTextComponents"]
            ]
            frozen_fields = IDENTITY_CONTRACT["frozenConflictEvaluationFields"]
            analysis[IDENTITY_SOURCE_FIELDS["frozenConflictEvaluations"]].append(
                {
                    frozen_fields["frozenValue"]: conflicting_value,
                    frozen_fields["conflict"]: True,
                    frozen_fields["componentIdentities"]: component_ids,
                    frozen_fields["explanation"]: "姓名和身份徽标都属于本次身份依赖闭包",
                }
            )
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["public"],
            source_transform=add_identity_conflict,
        )
        result = self.run_case("identity-content-frozen-during-replacement", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["explicitStrategyConflict"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_identity_topology_must_be_fully_covered_by_the_dependency_closure(self) -> None:
        def drop_required_component(analysis: dict) -> dict:
            analysis["dependencyClosure"] = analysis["dependencyClosure"][:-1]
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ip"],
            source_transform=drop_required_component,
        )
        result = self.run_case("incomplete-identity-dependency-closure", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_identity_topology_cannot_omit_a_component_that_remains_in_the_closure(self) -> None:
        def omit_repeated_component_from_topology(analysis: dict) -> dict:
            dependency_fields = IDENTITY_CONTRACT["dependencyFields"]
            component_field = dependency_fields["componentIdentity"]
            type_field = dependency_fields["dependencyType"]
            repeated_type = IDENTITY_CONTRACT["dependencyTypes"]["repeatedInstance"]
            repeated_component = next(
                item[component_field]
                for item in analysis["dependencyClosure"]
                if item[type_field] == repeated_type
            )
            topology_fields = IDENTITY_CONTRACT["topologyFields"]
            topology = analysis[IDENTITY_SOURCE_FIELDS["topology"]]
            topology[topology_fields["requiredComponents"]].remove(repeated_component)
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ip"],
            source_transform=omit_repeated_component_from_topology,
        )
        result = self.run_case("identity-topology-omits-repeated-instance", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_identity_topology_requires_a_full_body_dependency(self) -> None:
        def remove_full_body(analysis: dict) -> dict:
            dependency_fields = IDENTITY_CONTRACT["dependencyFields"]
            component_field = dependency_fields["componentIdentity"]
            type_field = dependency_fields["dependencyType"]
            full_body_type = IDENTITY_CONTRACT["dependencyTypes"]["fullBody"]
            removed_ids = {
                item[component_field]
                for item in analysis["dependencyClosure"]
                if item[type_field] == full_body_type
            }
            analysis["dependencyClosure"] = [
                item
                for item in analysis["dependencyClosure"]
                if item[component_field] not in removed_ids
            ]
            topology_fields = IDENTITY_CONTRACT["topologyFields"]
            topology = analysis[IDENTITY_SOURCE_FIELDS["topology"]]
            topology[topology_fields["requiredComponents"]] = [
                component_id
                for component_id in topology[topology_fields["requiredComponents"]]
                if component_id not in removed_ids
            ]
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ordinary"],
            source_transform=remove_full_body,
        )
        result = self.run_case("identity-topology-without-full-body", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_identity_text_topology_cannot_point_to_a_non_text_dependency(self) -> None:
        def relabel_shadow_as_identity_text(analysis: dict) -> dict:
            dependency_fields = IDENTITY_CONTRACT["dependencyFields"]
            component_field = dependency_fields["componentIdentity"]
            type_field = dependency_fields["dependencyType"]
            shadow_type = IDENTITY_CONTRACT["dependencyTypes"]["shadow"]
            shadow_component = next(
                item[component_field]
                for item in analysis["dependencyClosure"]
                if item[type_field] == shadow_type
            )
            topology_fields = IDENTITY_CONTRACT["topologyFields"]
            analysis[IDENTITY_SOURCE_FIELDS["topology"]][
                topology_fields["identityTextComponents"]
            ] = [shadow_component]
            decision_fields = IDENTITY_CONTRACT["identityTextDecisionFields"]
            analysis[IDENTITY_SOURCE_FIELDS["textDecisions"]][0][
                decision_fields["componentIdentity"]
            ] = shadow_component
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ip"],
            source_transform=relabel_shadow_as_identity_text,
        )
        result = self.run_case("identity-text-points-to-shadow", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_unsynchronized_identity_text_is_a_visual_hard_failure(self) -> None:
        scenarios = {
            "unsynchronized": {"identity_text_consistent": False},
            "legacy-residue": {"source_identity_terms_absent": False},
        }
        for name, overrides in scenarios.items():
            with self.subTest(failure=name):
                adapters = IdentityScenarioAdapters(SCENARIOS["public"], **overrides)
                result = self.run_case(f"identity-text-{name}", adapters)

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["visualHardFailure"], result.error_code)
                self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
                self.assertEqual([], adapters.upload_calls)

    def test_identity_review_cannot_claim_that_identity_text_is_not_applicable(self) -> None:
        adapters = IdentityScenarioAdapters(
            SCENARIOS["ip"],
            identity_text_applicable=False,
        )
        result = self.run_case("identity-review-applicability-mismatch", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_exposed_identity_text_requires_an_independent_neutrality_audit(self) -> None:
        specific_identity = copy.deepcopy(SCENARIOS["ip"])
        specific_identity["identityTextResult"] = "王力宏"
        adapters = IdentityScenarioAdapters(
            specific_identity,
            identity_neutral_defaults_valid=False,
        )
        result = self.run_case("specific-person-used-as-neutral-text", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_exposed_identity_text_must_compile_to_a_text_prompt_control(self) -> None:
        def turn_text_slot_into_subject_upload(analysis: dict) -> dict:
            identity_role = SLOT_CONTRACT["semanticRoles"]["identityText"]
            text_slot = next(
                slot
                for slot in analysis["slotCandidates"]
                if slot["semanticRole"] == identity_role
            )
            text_slot["type"] = "subject"
            text_slot.pop("exactVisibleText", None)
            text_slot.pop("exactVisibleTextEvidence", None)
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ip"],
            approved_transform=turn_text_slot_into_subject_upload,
        )
        result = self.run_case("identity-text-compiled-as-subject-upload", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_fixed_public_identity_may_use_the_synchronized_identity_outside_the_title(self) -> None:
        def use_synchronized_identity(analysis: dict) -> dict:
            analysis["neutralDescription"] = "林俊杰站在中央，重复倒影与身份徽标保持同步"
            analysis["promptTemplate"] += " 固定玩法中的主人物为林俊杰。"
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["public"],
            approved_transform=use_synchronized_identity,
        )
        result = self.run_case("fixed-public-identity-synchronized", adapters)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        formal = load_json(result.gallery_template)
        self.assertIn("林俊杰", formal["description"])
        self.assertIn("林俊杰", formal["promptTemplate"])
        self.assertNotIn("林俊杰", formal["title"])

    def test_synchronized_identity_text_can_use_a_bound_english_name(self) -> None:
        english_name = copy.deepcopy(SCENARIOS["public"])
        english_name["identityTextResult"] = "JJ LIN"
        english_name["identityTextRelationshipRole"] = "englishName"
        adapters = IdentityScenarioAdapters(english_name)
        result = self.run_case("public-identity-english-name", adapters)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        plan = load_json(result.output_dir / "replacement-plan.json")
        decision_fields = IDENTITY_CONTRACT["identityTextDecisionFields"]
        decision = plan[IDENTITY_PLAN_FIELDS["textDecisions"]][0]
        self.assertEqual("JJ LIN", decision[decision_fields["result"]])
        self.assertEqual(
            english_name["replacementValue"],
            decision[decision_fields["replacementIdentity"]],
        )

    def test_synchronized_identity_text_cannot_reuse_the_source_identity(self) -> None:
        stale_identity = copy.deepcopy(SCENARIOS["public"])
        stale_identity["identityTextResult"] = stale_identity["sourceIdentity"]
        stale_identity["identityTextRelationshipRole"] = "englishName"
        adapters = IdentityScenarioAdapters(stale_identity)
        result = self.run_case("public-identity-text-keeps-source-name", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_subject_upload_type_and_primary_subject_role_cannot_diverge(self) -> None:
        def split_subject_type_from_role(analysis: dict) -> dict:
            subject_slot = analysis["slotCandidates"][0]
            subject_slot["semanticRole"] = "custom_identity_upload"
            subject_slot["label"] = "皮卡丘主体上传"
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    gate: gate != VALUE_GATES["userDemand"]
                    for gate in VALUE_GATES.values()
                },
                "reason": "伪造省略证据以测试 type 与 semanticRole 的绑定",
            }
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ip"],
            approved_transform=split_subject_type_from_role,
        )
        result = self.run_case("subject-upload-role-divergence", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_open_identity_cannot_leak_into_non_identity_default_content(self) -> None:
        def leak_into_description(analysis: dict) -> dict:
            analysis["neutralDescription"] = "皮卡丘与重复剪影形成层次"
            return analysis

        def leak_into_fixed_prompt_content(analysis: dict) -> dict:
            analysis["promptTemplate"] += "背景还固定保留皮卡丘身份徽章。"
            return analysis

        def leak_into_slot_label(analysis: dict) -> dict:
            analysis["slotCandidates"][1]["label"] = "皮卡丘配饰"
            return analysis

        def leak_into_slot_placeholder(analysis: dict) -> dict:
            analysis["slotCandidates"][1]["placeholder"] = "输入皮卡丘风格"
            return analysis

        for name, transform in {
            "description": leak_into_description,
            "fixed-prompt-content": leak_into_fixed_prompt_content,
            "slot-label": leak_into_slot_label,
            "slot-placeholder": leak_into_slot_placeholder,
        }.items():
            with self.subTest(location=name):
                adapters = IdentityScenarioAdapters(
                    SCENARIOS["ip"],
                    approved_transform=transform,
                )
                result = self.run_case(f"identity-leaks-into-{name}", adapters)

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertEqual([], adapters.upload_calls)

    def test_person_attribute_slot_must_match_its_independent_assessment(self) -> None:
        clothing_role = PERSON_ATTRIBUTES["clothing"]

        def add_unapproved_clothing_slot(analysis: dict) -> dict:
            analysis["slotCandidates"].append(
                {
                    "id": "character_clothing",
                    "type": "prompt",
                    "semanticRole": clothing_role,
                    "label": "人物服装",
                    "placeholder": "描述服装",
                    "defaultValue": "深色夹克",
                    "suggestions": ["浅色衬衫", "红色卫衣", "蓝色工装"],
                    "hiddenConflictTokens": ["服装款式"],
                    "titleForbiddenTokens": ["深色夹克", "浅色衬衫", "红色卫衣", "蓝色工装"],
                    "valueGates": {gate: True for gate in VALUE_GATES.values()},
                }
            )
            analysis["promptTemplate"] = analysis["promptTemplate"].removesuffix("。") + (
                '，穿着{{ character_clothing | "深色夹克" }}。'
            )
            analysis["assetUnitAnalysis"][COUNT_FIELDS["controls"]] += 1
            return analysis

        adapters = IdentityScenarioAdapters(
            SCENARIOS["ordinary"],
            approved_transform=add_unapproved_clothing_slot,
        )
        result = self.run_case("attribute-slot-without-independent-approval", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)


if __name__ == "__main__":
    unittest.main()
