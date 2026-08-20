from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production
from tests.fixture_contracts import (
    rebuild_approved_component_graph,
    rebuild_source_component_graph_for_named_closure,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


RULES = load_json(ROOT / "contracts" / "machine-rules.json")
RESULT_COMPLETED = next(iter(RULES["resultStates"]))
RESULT_BLOCKED = tuple(RULES["resultStates"])[2]
RESULT_NEEDS_INPUT = tuple(RULES["resultStates"])[1]
RESULT_FAILED = tuple(RULES["resultStates"])[3]
STRATEGY_PER_IMAGE = RULES["strategySources"]["perImageDecision"]
STRATEGY_AUTONOMOUS = RULES["strategySources"]["autonomousDecision"]
SUGGESTION_AUDIT_CHECK = RULES["semanticAuditChecks"]["slotSuggestions"]["check"]
COUNT_FIELDS = RULES["slotCompilationContract"]["assetUnitCountFields"]


class ScenarioAdapters(DeterministicFixtureAdapters):
    def __init__(
        self,
        source_transform,
        approved_transform,
        identity_text_applicable=False,
    ):
        super().__init__(FIXTURE)
        self.source_transform = source_transform
        self.approved_transform = approved_transform
        self.identity_text_applicable = identity_text_applicable

    def analyze_source(self, source_image: Path, replacement_strategy: dict | None) -> dict:
        analysis = self.source_transform(
            super().analyze_source(source_image, replacement_strategy)
        )
        self.source_analysis = copy.deepcopy(analysis)
        return analysis

    def analyze_approved(self, approved_image: Path) -> dict:
        analysis = self.approved_transform(super().analyze_approved(approved_image))
        return rebuild_approved_component_graph(
            analysis, RULES, getattr(self, "source_analysis", None)
        )

    def inspect_generated(self, generated_image: Path, review_context: dict) -> dict:
        result = super().inspect_generated(generated_image, review_context)
        contract = RULES["visualReviewContract"]
        evidence_fields = contract["identityTextEvidenceFields"]
        identity_text = result[contract["evidenceFieldRoles"]["identityText"]]
        identity_text[evidence_fields["applicability"]] = self.identity_text_applicable
        evidence_payload = {
            field: result[field]
            for field in contract["evidenceFieldRoles"].values()
        }
        result["bindings"]["evidenceSha256"] = canonical_sha(evidence_payload)
        return result

    def audit_semantics(self, content: dict) -> dict:
        digest = hashlib.sha256(
            json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = super().audit_semantics(content)
        result["contentSha256"] = digest
        result["observedContentSha256"] = digest
        result["checks"] = {
            contract["check"]: True
            for contract in RULES["semanticAuditChecks"].values()
        }
        neutrality_contract = RULES["semanticAuditChecks"]["identityNeutrality"]
        neutrality_fields = RULES["identityReplacementContract"]["neutralityAuditFields"]
        subject_upload_type = RULES["slotCompilationContract"]["slotTypes"]["primarySubjectUpload"]
        neutrality_applicable = self.identity_text_applicable and any(
            slot["type"] == subject_upload_type for slot in content["slots"]
        )
        result["evidence"][neutrality_contract["evidence"]] = {
            neutrality_fields["applicability"]: neutrality_applicable,
            neutrality_fields["specificIdentityDetected"]: False,
            neutrality_fields["explanation"]: "逐项核对开放身份默认内容与非主体固定文案",
        }
        return result


def source_scenario(
    category: str,
    identity: str,
    role: str,
    valid_values: list[str],
    target_eligibility: dict[str, bool] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def transform(analysis: dict) -> dict:
        analysis["target"] = {"category": category, "role": role, "identity": identity}
        analysis["replacementPool"] = [
            {
                "value": value,
                "category": category,
                "semanticCompatible": True,
                "visualCompatible": True,
                "rightsAndSafety": "pass",
                "score": 0.95 - index * 0.1,
                "reason": "同类别且保持画面机制",
            }
            for index, value in enumerate(valid_values)
        ]
        analysis["replacementPool"].append(
            {
                "value": "跨类别干扰项",
                "category": RULES["sourceCategories"]["genericAnimal"],
                "semanticCompatible": False,
                "visualCompatible": False,
                "rightsAndSafety": "pass",
                "score": 0.99,
                "reason": "类别与画面职责不兼容",
            }
        )
        identity_contract = RULES["identityReplacementContract"]
        dependency_fields = identity_contract["dependencyFields"]
        multi_contract = RULES["multiInstanceContract"]
        dependency_role = (
            multi_contract["operationDependencyTypes"]["sceneReplace"]
            if category == RULES["sourceCategories"]["sceneAttribute"]
            else multi_contract["operationDependencyTypes"]["maskFill"]
            if category
            in {
                RULES["sourceCategories"]["textContent"],
                RULES["sourceCategories"]["genericObject"],
                RULES["sourceCategories"]["genericFood"],
            }
            else "fullBody"
        )
        dependency_type = identity_contract["dependencyTypes"][dependency_role]
        analysis["dependencyClosure"] = [
            {
                dependency_fields["componentIdentity"]: "primary-target-region",
                dependency_fields["dependencyType"]: dependency_type,
                dependency_fields["description"]: f"{role}及其边缘、阴影和重复区域",
            }
        ]
        identity_route = next(
            (
                route
                for route in identity_contract["routes"].values()
                if category == RULES["sourceCategories"][route["sourceCategoryRole"]]
            ),
            None,
        )
        if identity_route is not None:
            distinct_field = identity_contract["candidateFields"]["distinctIdentityEvidence"]
            distinct_fields = identity_contract["distinctIdentityEvidenceFields"]
            for candidate in analysis["replacementPool"]:
                if candidate["category"] == category:
                    candidate[distinct_field] = {
                        distinct_fields["sourceIdentity"]: identity,
                        distinct_fields["candidateIdentity"]: candidate["value"],
                        distinct_fields["distinct"]: True,
                        distinct_fields["explanation"]: "确认候选与来源身份不同且保持同类语境",
                    }
            dependency_types = identity_contract["dependencyTypes"]
            component_field = dependency_fields["componentIdentity"]
            type_field = dependency_fields["dependencyType"]
            value_field = dependency_fields["description"]
            analysis["dependencyClosure"] = [
                {
                    component_field: "primary-identity-body",
                    type_field: dependency_types["fullBody"],
                    value_field: f"{identity}的完整人物区域",
                },
                {
                    component_field: "primary-identity-text",
                    type_field: dependency_types["identityText"],
                    value_field: f"{identity}的身份文字",
                },
            ]
            route_fields = identity_contract["routeEvidenceFields"]
            analysis[identity_contract["sourceFields"]["routeEvidence"]] = {
                route_fields["mode"]: identity_route["mode"],
                route_fields["localAssetRequirement"]: identity_route["localAssetRequired"],
                route_fields["completeRedraw"]: True,
                route_fields["explanation"]: "完整身份与依赖区域统一重绘",
            }
            topology_fields = identity_contract["topologyFields"]
            analysis[identity_contract["sourceFields"]["topology"]] = {
                topology_fields["requiredComponents"]: [
                    "primary-identity-body",
                    "primary-identity-text",
                ],
                topology_fields["identityTextComponents"]: ["primary-identity-text"],
                topology_fields["explanation"]: "主体与身份文字均属于变更集",
            }
            decision_fields = identity_contract["identityTextDecisionFields"]
            analysis[identity_contract["sourceFields"]["textDecisions"]] = [
                {
                    decision_fields["componentIdentity"]: "primary-identity-text",
                    decision_fields["sourceText"]: identity,
                    decision_fields["action"]: identity_contract["identityTextActions"]["remove"],
                    decision_fields["result"]: "",
                    decision_fields["basis"]: "开放主体不保留具体身份文字",
                }
            ]
            if identity_route["candidateCardRequired"]:
                card_field = identity_contract["candidateFields"]["card"]
                card_fields = identity_contract["candidateCardFields"]
                for candidate in analysis["replacementPool"]:
                    if candidate["category"] == category:
                        candidate[card_field] = {
                            card_fields["anchors"]: ["新角色轮廓", "新角色配色"],
                            card_fields["antiAnchors"]: ["不残留旧角色特征"],
                            card_fields["playFusion"]: ["保持桌边姿态和媒介"],
                        }
        analysis["frozenSet"] = ["构图骨架", "核心关系", "原有媒介"]
        if identity_route is not None:
            source_fields = identity_contract["sourceFields"]
            frozen_fields = identity_contract["frozenConflictEvaluationFields"]
            analysis[source_fields["frozenConflictEvaluations"]] = [
                {
                    frozen_fields["frozenValue"]: value,
                    frozen_fields["conflict"]: False,
                    frozen_fields["componentIdentities"]: [],
                    frozen_fields["explanation"]: "逐项对照身份拓扑后未发现冲突",
                }
                for value in analysis["frozenSet"]
            ]
        if target_eligibility is not None:
            analysis["targetEligibility"] = target_eligibility
        rebuild_source_component_graph_for_named_closure(analysis, RULES)
        return analysis

    return transform


def approved_scenario(
    *,
    title: str,
    subject_id: str,
    subject_default: str,
    subject_suggestions: list[str],
    subject_role: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def transform(analysis: dict) -> dict:
        slot_contract = RULES["slotCompilationContract"]
        semantic_roles = slot_contract["semanticRoles"]
        subject_semantic_role = (
            semantic_roles["primarySubject"]
            if subject_role == "subject"
            else semantic_roles["primaryVisualText"]
            if subject_role == "text"
            else semantic_roles["sceneContent"]
        )
        subject_slot_type = (
            slot_contract["slotTypes"]["primarySubjectUpload"]
            if subject_role == "subject"
            else slot_contract["slotTypes"]["visibleTextPrompt"]
            if subject_role == "text"
            else slot_contract["slotTypes"]["freePrompt"]
        )
        analysis["neutralTitle"] = title
        analysis["titleEvidence"] = {
            "templateGrounded": True,
            "usageMotivation": True,
            "spokenNaturalness": True,
            "slotPortability": True,
            "evidence": f"标题“{title}”只概括当前场景的可见机制，三个开放轴替换后仍成立",
        }
        analysis["neutralDescription"] = "主要可编辑区域位于画面核心，外围留白与空间层级清晰"
        analysis["hasPrimarySubject"] = subject_role == "subject"
        first, second, third = analysis["slotCandidates"]
        first.update(
            {
                "id": subject_id,
                "type": subject_slot_type,
                "semanticRole": subject_semantic_role,
                "label": "主要替换内容",
                "defaultValue": subject_default,
                "suggestions": subject_suggestions,
                "hiddenConflictTokens": ["主要内容固定"],
                "titleForbiddenTokens": [subject_default, *subject_suggestions],
            }
        )
        if subject_role != "subject":
            first.pop("identityInheritanceDecision", None)
        second.update(
            {
                "id": "supporting_look",
                "semanticRole": semantic_roles["supportingAppearance"],
                "defaultValue": "柔和中性色",
                "suggestions": ["低饱和蓝色", "温暖米色", "清透绿色"],
                "hiddenConflictTokens": ["配色固定"],
                "titleForbiddenTokens": ["中性色", "蓝色", "米色", "绿色"],
            }
        )
        third.update(
            {
                "id": "ambient_light",
                "semanticRole": semantic_roles["sceneAtmosphere"],
                "defaultValue": "侧面柔光",
                "suggestions": ["清晨薄光", "阴天漫射光", "暖色夜灯"],
                "hiddenConflictTokens": ["光线固定"],
                "titleForbiddenTokens": ["侧面柔光", "清晨", "阴天", "夜灯"],
            }
        )
        analysis["promptTemplate"] = (
            f"{{{{ {subject_id} | \"{subject_default}\" }}}}位于画面核心区域，"
            "以{{ supporting_look | \"柔和中性色\" }}呈现，"
            "{{ ambient_light | \"侧面柔光\" }}塑造层次，保留构图骨架、核心关系和留白节奏。"
        )
        analysis["freeEditableContent"] = ["构图骨架", "核心关系", "留白节奏"]
        if subject_role != "subject":
            analysis["assetUnitAnalysis"].update(
                {
                    COUNT_FIELDS["visibleSubjects"]: 0,
                    COUNT_FIELDS["identities"]: 0,
                    COUNT_FIELDS["uploads"]: 0,
                }
            )
        analysis["runtimeSemantics"]["visualContract"].update(
            {
                "medium": (
                    "平面文字海报设计，主文字字形边缘清晰"
                    if subject_role == "text"
                    else "写实环境摄影，前景与背景层次清晰"
                    if subject_role == "scene"
                    else "写实主题摄影，主要目标轮廓与环境边界清晰"
                ),
                "styleTraits": ["轮廓边缘干净，表面纹理与画面细节密度保持统一"],
                "composition": ["主要可编辑区域位于画面核心，固定边界与外围留白形成清晰层级"],
                "relations": ["被替换区域只接管已声明目标，固定环境与其他目标不接受身份扩散"],
                "colorAndLight": [],
            }
        )
        if subject_role == "text":
            text_contract = RULES["visibleTextContract"]
            region_fields = text_contract["regionFields"]
            evidence_fields = text_contract["exactEvidenceFields"]
            region_id = "main-text-region"
            first[text_contract["slotBindingField"]] = region_id
            first["exactVisibleText"] = True
            first["exactVisibleTextEvidence"] = {
                "approvedImageSha256": analysis["visualFactSourceSha256"],
                "visibleText": subject_default,
                "evidence": "确认模板图中的主文字已逐字绑定",
            }
            analysis[text_contract["analysisFields"]["regions"]] = [
                {
                    region_fields["identity"]: region_id,
                    region_fields["sourceText"]: subject_default,
                    region_fields["role"]: text_contract["roles"]["content"],
                    region_fields["valueClass"]: text_contract["valueClasses"]["primaryVisual"],
                    region_fields["action"]: text_contract["actions"]["openSlot"],
                    region_fields["slotIdentity"]: subject_id,
                    region_fields["selectedText"]: subject_default,
                    region_fields["exactTextEvidence"]: {
                        evidence_fields["language"]: text_contract["languageValues"]["simplifiedChinese"],
                        evidence_fields["tokens"]: [subject_default],
                        evidence_fields["lines"]: [subject_default],
                        evidence_fields["caseSensitiveTokens"]: [],
                        evidence_fields["rareSymbols"]: [],
                        evidence_fields["symbolTopology"]: "单行主文字",
                        evidence_fields["explanation"]: "逐字、逐行核对主文字",
                    },
                }
            ]
            inventory_fields = text_contract["inventoryFields"]
            analysis[text_contract["analysisFields"]["inventory"]] = {
                inventory_fields["complete"]: True,
                inventory_fields["regionIdentities"]: [region_id],
                inventory_fields["explanation"]: "全画布仅有一个主文字区域",
            }
        return analysis

    return transform


class Issue3ReplacementStrategyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_per_image_value_overrides_autonomous_choice_and_defaults_complete_the_closure(self) -> None:
        request = {
            **self.request,
            "productionItemId": "per-image-water-capybara",
            "replacementStrategy": {
                "policyId": "user-animal-choice",
                "policyVersion": "1",
                "replacementValue": "水豚",
                "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
            },
        }

        result = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        self.assertEqual(STRATEGY_PER_IMAGE, plan["strategy"]["source"])
        self.assertEqual("user-animal-choice", plan["strategy"]["policyId"])
        self.assertEqual("水豚", plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual(STRATEGY_PER_IMAGE, plan["primaryTargets"][0]["decisionSource"])
        self.assertEqual(
            {STRATEGY_AUTONOMOUS},
            {item["decisionSource"] for item in plan["dependencyClosure"]},
        )

    def test_per_image_strategy_does_not_leak_to_another_production_item(self) -> None:
        explicit_request = {
            **self.request,
            "productionItemId": "isolated-explicit-item",
            "replacementStrategy": {
                "replacementValue": "水豚",
                "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
            },
        }
        autonomous_request = {**self.request, "productionItemId": "isolated-autonomous-item"}
        adapters = DeterministicFixtureAdapters(FIXTURE)

        explicit = run_production(explicit_request, self.output_root, adapters, clock=lambda: FIXED_TIME)
        autonomous = run_production(autonomous_request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_COMPLETED, explicit.outcome)
        self.assertEqual(RESULT_COMPLETED, autonomous.outcome)
        explicit_plan = load_json(explicit.output_dir / "replacement-plan.json")
        autonomous_plan = load_json(autonomous.output_dir / "replacement-plan.json")
        self.assertEqual("水豚", explicit_plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual("柯基犬", autonomous_plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual(STRATEGY_AUTONOMOUS, autonomous_plan["strategy"]["source"])

    def test_same_input_strategy_and_version_produce_the_same_replacement_plan(self) -> None:
        strategy = {
            "policyId": "stable-animal-policy",
            "policyVersion": "1",
            "forbidValues": ["柯基犬"],
        }
        first_request = {
            **self.request,
            "productionItemId": "stable-plan-first",
            "replacementStrategy": strategy,
        }
        second_request = {
            **self.request,
            "productionItemId": "stable-plan-second",
            "replacementStrategy": strategy,
        }

        first = run_production(
            first_request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        second = run_production(
            second_request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, first.outcome)
        self.assertEqual(RESULT_COMPLETED, second.outcome)
        self.assertEqual(
            (first.output_dir / "replacement-plan.json").read_bytes(),
            (second.output_dir / "replacement-plan.json").read_bytes(),
        )

    def test_completed_production_item_cannot_be_reused_with_a_different_strategy(self) -> None:
        item_id = "strategy-bound-production-item"
        first_request = {
            **self.request,
            "productionItemId": item_id,
            "replacementStrategy": {
                "replacementValue": "水豚",
                "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
            },
        }
        second_request = {
            **self.request,
            "productionItemId": item_id,
            "replacementStrategy": {
                "replacementValue": "柯基犬",
                "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
            },
        }
        run_production(
            first_request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed = run_production(second_request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, resumed.outcome)
        self.assertEqual(RULES["errorCodes"]["productionItemIntegrityFailure"], resumed.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_reordered_constraint_lists_keep_the_same_strategy_identity(self) -> None:
        item_id = "normalized-strategy-identity"
        first_request = {
            **self.request,
            "productionItemId": item_id,
            "replacementStrategy": {"forbidValues": ["柯基犬", "不存在的值"]},
        }
        second_request = {
            **self.request,
            "productionItemId": item_id,
            "replacementStrategy": {"forbidValues": ["不存在的值", "柯基犬"]},
        }
        run_production(
            first_request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        resumed = run_production(second_request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_COMPLETED, resumed.outcome)
        self.assertTrue(resumed.resumed)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_incompatible_per_image_value_is_blocked_before_generation(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {
            **self.request,
            "productionItemId": "incompatible-explicit-strategy",
            "replacementStrategy": {
                "replacementValue": "马克杯",
                "replacementCategory": RULES["sourceCategories"]["genericObject"],
            },
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["explicitStrategyConflict"], result.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((result.output_dir / "replacement-plan.json").exists())

    def test_incomplete_per_image_strategy_is_rejected_before_analysis_or_output(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {
            **self.request,
            "productionItemId": "incomplete-explicit-strategy",
            "replacementStrategy": {"replacementValue": "水豚"},
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_NEEDS_INPUT, result.outcome)
        self.assertEqual(RULES["errorCodes"]["invalidProductionRequest"], result.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((self.output_root / "incomplete-explicit-strategy").exists())

    def test_unknown_category_requires_review_before_generation(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original = adapters.analyze_source

        def unknown_source(path: Path, replacement_strategy: dict | None) -> dict:
            analysis = copy.deepcopy(original(path, replacement_strategy))
            analysis["target"]["category"] = RULES["sourceCategories"]["unknownCategory"]
            return analysis

        adapters.analyze_source = unknown_source
        request = {**self.request, "productionItemId": "unknown-source-category"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_NEEDS_INPUT, result.outcome)
        self.assertEqual(RULES["errorCodes"]["unknownCategory"], result.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_partial_per_image_constraints_leave_uncovered_target_choice_autonomous(self) -> None:
        request = {
            **self.request,
            "productionItemId": "partial-per-image-strategy",
            "replacementStrategy": {
                "policyId": "exclude-first-choice",
                "forbidValues": ["柯基犬"],
            },
        }

        result = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        self.assertEqual(STRATEGY_PER_IMAGE, plan["strategy"]["source"])
        self.assertEqual(["柯基犬"], plan["strategy"]["forbidValues"])
        self.assertEqual("水豚", plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual(STRATEGY_AUTONOMOUS, plan["primaryTargets"][0]["decisionSource"])

    def test_per_image_preserve_items_extend_frozen_set_without_owning_target_choice(self) -> None:
        request = {
            **self.request,
            "productionItemId": "per-image-preserve-strategy",
            "replacementStrategy": {
                "policyId": "keep-language",
                "preserve": ["窗边标签保持中文", "摄影媒介与浅景深"],
            },
        }

        result = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        self.assertEqual(STRATEGY_AUTONOMOUS, plan["primaryTargets"][0]["decisionSource"])
        self.assertIn("窗边标签保持中文", plan["frozenSet"])
        self.assertEqual(1, plan["frozenSet"].count("摄影媒介与浅景深"))
        self.assertIn(
            {"value": "窗边标签保持中文", "decisionSource": STRATEGY_PER_IMAGE},
            plan["frozenSetDecisions"],
        )
        self.assertIn(
            {"value": "摄影媒介与浅景深", "decisionSource": STRATEGY_PER_IMAGE},
            plan["frozenSetDecisions"],
        )

    def test_replacement_pool_and_slot_suggestion_pool_have_separate_sidecars_and_invariants(self) -> None:
        request = {**self.request, "productionItemId": "separate-value-pools"}

        result = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        editable = load_json(result.output_dir / "editable-template-spec.json")
        formal = load_json(result.output_dir / "gallery-template.json")
        self.assertIn("replacementPool", plan)
        self.assertNotIn("slotSuggestionPools", plan)
        self.assertIn("slotSuggestionPools", editable)
        self.assertNotIn("replacementPool", editable)
        self.assertNotIn("replacementPool", formal)
        for slot in editable["slots"]:
            self.assertNotIn(slot["defaultValue"], slot["suggestions"])
            self.assertEqual(len(slot["suggestions"]), len(set(slot["suggestions"])))

    def test_slot_suggestions_cannot_repeat_the_default_or_each_other(self) -> None:
        def duplicate_suggestions(analysis: dict) -> dict:
            default = analysis["slotCandidates"][0]["defaultValue"]
            analysis["slotCandidates"][0]["suggestions"] = [default, default, "垂耳兔"]
            return analysis

        adapters = ScenarioAdapters(lambda analysis: analysis, duplicate_suggestions)
        request = {**self.request, "productionItemId": "duplicate-slot-suggestions"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_non_list_slot_suggestions_are_reported_as_a_contract_failure(self) -> None:
        def non_list_suggestions(analysis: dict) -> dict:
            analysis["slotCandidates"][0]["suggestions"] = None
            return analysis

        adapters = ScenarioAdapters(lambda analysis: analysis, non_list_suggestions)
        request = {**self.request, "productionItemId": "non-list-slot-suggestions"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_semantic_audit_blocks_cross_axis_or_wrong_granularity_suggestions(self) -> None:
        def cross_axis_suggestions(analysis: dict) -> dict:
            analysis["slotCandidates"][0]["suggestions"] = [
                "几何抽象版本", "复古版本", "柔光摄影版本"
            ]
            return analysis

        adapters = ScenarioAdapters(lambda analysis: analysis, cross_axis_suggestions)
        original_audit = adapters.audit_semantics
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        def cross_axis_audit(content: dict) -> dict:
            audit = original_audit(content)
            audit["evidence"][evidence_field][0]["suggestionReviews"][0][
                "sameAxis"
            ] = False
            return audit

        adapters.audit_semantics = cross_axis_audit
        request = {**self.request, "productionItemId": "cross-axis-slot-suggestions"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)
        audit = load_json(result.output_dir / "semantic-audit.json")
        self.assertTrue(audit["checks"][SUGGESTION_AUDIT_CHECK])
        self.assertFalse(
            audit["evidence"][evidence_field][0]["suggestionReviews"][0][
                "sameAxis"
            ]
        )

    def test_deterministic_adapter_preserves_fixture_authored_axis_and_granularity(
        self,
    ) -> None:
        fixture = self.output_root / "authored-semantic-fixture"
        shutil.copytree(FIXTURE, fixture)
        audit_path = fixture / "semantic-audit.json"
        audit = load_json(audit_path)
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]
        audit["evidence"][evidence_field][0]["axis"] = "逐图定义的角色身份轴"
        audit["evidence"][evidence_field][0]["granularity"] = "单个动物角色"
        audit_path.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        request = {
            **self.request,
            "productionItemId": "fixture-authored-suggestion-semantics",
        }

        result = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(fixture),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        recorded = load_json(result.output_dir / "semantic-audit.json")
        review = recorded["evidence"][evidence_field][0]
        self.assertEqual("逐图定义的角色身份轴", review["axis"])
        self.assertEqual("单个动物角色", review["granularity"])

    def test_semantic_audit_rejects_legacy_slot_id_only_suggestion_reviews(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_audit = adapters.audit_semantics
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        def legacy_audit(content: dict) -> dict:
            audit = original_audit(content)
            audit["evidence"][evidence_field] = [
                slot["id"] for slot in content["slots"]
            ]
            return audit

        adapters.audit_semantics = legacy_audit
        request = {
            **self.request,
            "productionItemId": "legacy-slot-id-only-suggestion-reviews",
        }

        result = run_production(
            request,
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_semantic_audit_accepts_structured_per_suggestion_reviews(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {
            **self.request,
            "productionItemId": "structured-per-suggestion-reviews",
        }

        result = run_production(
            request,
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        editable = load_json(result.output_dir / "editable-template-spec.json")
        audit = load_json(result.output_dir / "semantic-audit.json")
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]
        reviews = audit["evidence"][evidence_field]
        self.assertEqual(
            [slot["id"] for slot in editable["slots"]],
            [review["slotId"] for review in reviews],
        )
        for slot, review in zip(editable["slots"], reviews, strict=True):
            self.assertEqual(slot["defaultValue"], review["defaultValue"])
            self.assertTrue(review["axis"].strip())
            self.assertTrue(review["granularity"].strip())
            self.assertTrue(review["evidence"].strip())
            self.assertEqual(
                slot["suggestions"],
                [item["value"] for item in review["suggestionReviews"]],
            )
            for item in review["suggestionReviews"]:
                self.assertIs(item["sameAxis"], True)
                self.assertIs(item["sameGranularity"], True)
                self.assertIs(item["mechanismCompatible"], True)
                self.assertTrue(item["evidence"].strip())

    def test_structured_suggestion_reviews_bind_the_current_default_value(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_audit = adapters.audit_semantics
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        def mismatched_default_audit(content: dict) -> dict:
            audit = original_audit(content)
            audit["evidence"][evidence_field][0]["defaultValue"] = "另一模板默认值"
            return audit

        adapters.audit_semantics = mismatched_default_audit
        result = run_production(
            {
                **self.request,
                "productionItemId": "suggestion-review-default-mismatch",
            },
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_structured_suggestion_reviews_bind_every_current_suggestion(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_audit = adapters.audit_semantics
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        def mismatched_suggestion_audit(content: dict) -> dict:
            audit = original_audit(content)
            audit["evidence"][evidence_field][0]["suggestionReviews"][0][
                "value"
            ] = "另一模板推荐值"
            return audit

        adapters.audit_semantics = mismatched_suggestion_audit
        result = run_production(
            {
                **self.request,
                "productionItemId": "suggestion-review-value-mismatch",
            },
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_structured_suggestion_reviews_require_positive_grounded_claims(self) -> None:
        cases = (
            ("blank-axis", lambda reviews: reviews[0].__setitem__("axis", "")),
            (
                "blank-granularity",
                lambda reviews: reviews[0].__setitem__("granularity", ""),
            ),
            (
                "blank-slot-evidence",
                lambda reviews: reviews[0].__setitem__("evidence", ""),
            ),
            (
                "cross-axis",
                lambda reviews: reviews[0]["suggestionReviews"][0].__setitem__(
                    "sameAxis", False
                ),
            ),
            (
                "wrong-granularity",
                lambda reviews: reviews[0]["suggestionReviews"][0].__setitem__(
                    "sameGranularity", False
                ),
            ),
            (
                "mechanism-incompatible",
                lambda reviews: reviews[0]["suggestionReviews"][0].__setitem__(
                    "mechanismCompatible", False
                ),
            ),
            (
                "blank-item-evidence",
                lambda reviews: reviews[0]["suggestionReviews"][0].__setitem__(
                    "evidence", ""
                ),
            ),
        )
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        for case_name, mutate in cases:
            with self.subTest(case=case_name):
                adapters = DeterministicFixtureAdapters(FIXTURE)
                original_audit = adapters.audit_semantics

                def invalid_claim_audit(content: dict, mutate=mutate) -> dict:
                    audit = original_audit(content)
                    mutate(audit["evidence"][evidence_field])
                    return audit

                adapters.audit_semantics = invalid_claim_audit
                result = run_production(
                    {
                        **self.request,
                        "productionItemId": f"suggestion-review-{case_name}",
                    },
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RESULT_BLOCKED, result.outcome)
                self.assertEqual(
                    RULES["errorCodes"]["contractFailure"], result.error_code
                )
                self.assertEqual([], adapters.upload_calls)

    def test_structured_suggestion_reviews_reject_duplicate_slot_coverage(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        original_audit = adapters.audit_semantics
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        def duplicate_slot_audit(content: dict) -> dict:
            audit = original_audit(content)
            reviews = audit["evidence"][evidence_field]
            reviews.append(copy.deepcopy(reviews[0]))
            return audit

        adapters.audit_semantics = duplicate_slot_audit
        result = run_production(
            {
                **self.request,
                "productionItemId": "duplicate-suggestion-slot-review",
            },
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_structured_suggestion_reviews_reject_shape_and_coverage_drift(self) -> None:
        evidence_field = RULES["semanticAuditChecks"]["slotSuggestions"]["evidence"]

        cases = (
            ("missing-slot", lambda reviews: reviews.pop()),
            (
                "unknown-slot",
                lambda reviews: reviews[0].__setitem__("slotId", "unknown-slot"),
            ),
            (
                "unknown-slot-field",
                lambda reviews: reviews[0].__setitem__("unexpected", True),
            ),
            (
                "unknown-suggestion-field",
                lambda reviews: reviews[0]["suggestionReviews"][0].__setitem__(
                    "unexpected", True
                ),
            ),
        )

        for case_name, mutate in cases:
            with self.subTest(case=case_name):
                adapters = DeterministicFixtureAdapters(FIXTURE)
                original_audit = adapters.audit_semantics

                def invalid_shape_audit(content: dict, mutate=mutate) -> dict:
                    audit = original_audit(content)
                    mutate(audit["evidence"][evidence_field])
                    return audit

                adapters.audit_semantics = invalid_shape_audit
                result = run_production(
                    {
                        **self.request,
                        "productionItemId": f"suggestion-review-{case_name}",
                    },
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RESULT_BLOCKED, result.outcome)
                self.assertEqual(
                    RULES["errorCodes"]["contractFailure"], result.error_code
                )
                self.assertEqual([], adapters.upload_calls)

    def test_preserve_cannot_freeze_a_value_required_by_the_dependency_closure(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {
            **self.request,
            "productionItemId": "preserve-changed-set-conflict",
            "replacementStrategy": {"preserve": ["身体压在软垫上的接触阴影"]},
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["explicitStrategyConflict"], result.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_preserve_cannot_freeze_the_source_identity_being_replaced(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {
            **self.request,
            "productionItemId": "preserve-source-identity-conflict",
            "replacementStrategy": {"preserve": ["橘猫"]},
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["explicitStrategyConflict"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_preserve_semantic_conflict_evidence_blocks_paraphrased_changed_content(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = {
            **self.request,
            "productionItemId": "paraphrased-preserve-conflict",
            "replacementStrategy": {"preserve": ["保持身体压在软垫上的接触阴影"]},
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["explicitStrategyConflict"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_explicit_value_can_be_hard_filtered_without_belonging_to_the_autonomous_pool(self) -> None:
        def explicit_evaluation(analysis: dict) -> dict:
            analysis["explicitReplacementEvaluation"] = {
                "value": "羊驼",
                "category": RULES["sourceCategories"]["genericAnimal"],
                "semanticCompatible": True,
                "visualCompatible": True,
                "rightsAndSafety": "pass",
                "score": 0.91,
                "reason": "同类动物、伏卧姿态与画面接触关系可实现",
            }
            return analysis

        adapters = ScenarioAdapters(explicit_evaluation, lambda analysis: analysis)
        request = {
            **self.request,
            "productionItemId": "explicit-value-outside-autonomous-pool",
            "replacementStrategy": {
                "replacementValue": "羊驼",
                "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
            },
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        self.assertEqual("羊驼", plan["primaryTargets"][0]["replacementValue"])
        self.assertNotIn("羊驼", [item["value"] for item in plan["replacementPool"]])

    def test_incomplete_explicit_value_evaluation_is_a_stable_strategy_conflict(self) -> None:
        def incomplete_evaluation(analysis: dict) -> dict:
            analysis["explicitReplacementEvaluation"] = {
                "value": "羊驼",
                "category": RULES["sourceCategories"]["genericAnimal"],
                "semanticCompatible": True,
                "visualCompatible": True,
                "rightsAndSafety": "pass",
            }
            return analysis

        adapters = ScenarioAdapters(incomplete_evaluation, lambda analysis: analysis)
        request = {
            **self.request,
            "productionItemId": "incomplete-explicit-evaluation",
            "replacementStrategy": {
                "replacementValue": "羊驼",
                "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
            },
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["explicitStrategyConflict"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_source_adapter_cannot_mutate_the_strategy_used_for_identity_and_planning(self) -> None:
        class MutatingStrategyAdapters(DeterministicFixtureAdapters):
            def analyze_source(self, source_image: Path, replacement_strategy: dict | None) -> dict:
                original_strategy = copy.deepcopy(replacement_strategy)
                assert replacement_strategy is not None
                replacement_strategy["replacementValue"] = "柯基犬"
                return super().analyze_source(source_image, original_strategy)

        strategy = {
            "replacementValue": "水豚",
            "replacementCategory": RULES["sourceCategories"]["genericAnimal"],
        }
        request = {
            **self.request,
            "productionItemId": "adapter-cannot-mutate-strategy",
            "replacementStrategy": strategy,
        }

        result = run_production(
            request,
            self.output_root,
            MutatingStrategyAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        manifest = load_json(result.output_dir / "production-manifest.json")
        expected_sha = hashlib.sha256(
            json.dumps(strategy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual("水豚", plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual(expected_sha, manifest["replacementStrategySha256"])

    def test_non_object_target_eligibility_is_a_stable_external_failure(self) -> None:
        def invalid_eligibility(analysis: dict) -> dict:
            analysis = source_scenario(
                RULES["sourceCategories"]["textContent"],
                "来源主标题",
                "画面中央的主要文字",
                ["今天也要加油", "周末继续躺平"],
            )(analysis)
            analysis["targetEligibility"] = None
            return analysis

        adapters = ScenarioAdapters(invalid_eligibility, lambda analysis: analysis)
        request = {**self.request, "productionItemId": "invalid-target-eligibility"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_FAILED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_rights_uncertainty_requires_review_instead_of_reporting_incompatibility(self) -> None:
        def rights_need_review(analysis: dict) -> dict:
            for candidate in analysis["replacementPool"]:
                if candidate["category"] == analysis["target"]["category"]:
                    candidate["rightsAndSafety"] = "review"
            return analysis

        adapters = ScenarioAdapters(rights_need_review, lambda analysis: analysis)
        request = {**self.request, "productionItemId": "rights-risk-needs-review"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_NEEDS_INPUT, result.outcome)
        self.assertEqual(RULES["errorCodes"].get("riskNeedsReview"), result.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_preserve_conflict_evidence_must_bind_boolean_to_current_changed_component_ids(self) -> None:
        def inconsistent_conflict_evidence(analysis: dict) -> dict:
            evaluation = analysis["preserveConflictEvaluations"][0]
            evaluation["conflictsWithChangedSet"] = False
            evaluation["changedComponentIds"] = ["dependency-2-shadow"]
            return analysis

        adapters = ScenarioAdapters(inconsistent_conflict_evidence, lambda analysis: analysis)
        request = {
            **self.request,
            "productionItemId": "inconsistent-preserve-conflict-evidence",
            "replacementStrategy": {"preserve": ["摄影媒介与浅景深"]},
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_FAILED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_malformed_dependency_closure_is_a_stable_external_failure(self) -> None:
        def malformed_closure(analysis: dict) -> dict:
            analysis["replacementPool"] = []
            analysis["dependencyClosure"] = [{"type": "shadow"}]
            return analysis

        adapters = ScenarioAdapters(malformed_closure, lambda analysis: analysis)
        request = {**self.request, "productionItemId": "malformed-dependency-closure"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_FAILED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_empty_dependency_closure_requires_range_review(self) -> None:
        def empty_closure(analysis: dict) -> dict:
            analysis["dependencyClosure"] = []
            return analysis

        adapters = ScenarioAdapters(empty_closure, lambda analysis: analysis)
        request = {**self.request, "productionItemId": "empty-dependency-closure"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_NEEDS_INPUT, result.outcome)
        self.assertEqual(RULES["errorCodes"]["riskNeedsReview"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_text_target_without_authorization_or_mechanism_need_is_blocked(self) -> None:
        adapters = ScenarioAdapters(
            source_scenario(
                RULES["sourceCategories"]["textContent"],
                "来源主标题",
                "画面中央的主要文字",
                ["今天也要加油", "周末继续躺平"],
            ),
            lambda analysis: analysis,
        )
        request = {**self.request, "productionItemId": "unauthorized-text-target"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["noCompatibleReplacement"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_scene_target_is_blocked_when_the_primary_subject_still_has_replacement_value(self) -> None:
        def scene_with_replaceable_subject(analysis: dict) -> dict:
            analysis = source_scenario(
                RULES["sourceCategories"]["sceneAttribute"],
                "阴天客厅",
                "窗外环境与室内光线",
                ["霓虹夜景", "雪天清晨"],
            )(analysis)
            analysis["targetEligibility"] = {
                "primarySubjectHasReplacementValue": True,
                "sceneChangeCreatesStableTemplateValue": True,
            }
            return analysis

        adapters = ScenarioAdapters(scene_with_replaceable_subject, lambda analysis: analysis)
        request = {**self.request, "productionItemId": "scene-with-replaceable-subject"}

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_BLOCKED, result.outcome)
        self.assertEqual(RULES["errorCodes"]["noCompatibleReplacement"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_explicit_scene_value_overrides_the_autonomous_scene_eligibility_rule(self) -> None:
        def explicit_scene(analysis: dict) -> dict:
            analysis = source_scenario(
                RULES["sourceCategories"]["sceneAttribute"],
                "阴天客厅",
                "窗外环境与室内光线",
                ["霓虹夜景", "雪天清晨"],
                {
                    "primarySubjectHasReplacementValue": True,
                    "sceneChangeCreatesStableTemplateValue": True,
                },
            )(analysis)
            analysis["explicitReplacementEvaluation"] = copy.deepcopy(analysis["replacementPool"][0])
            return analysis

        adapters = ScenarioAdapters(explicit_scene, lambda analysis: analysis)
        request = {
            **self.request,
            "productionItemId": "explicit-scene-override",
            "replacementStrategy": {
                "replacementValue": "霓虹夜景",
                "replacementCategory": RULES["sourceCategories"]["sceneAttribute"],
            },
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RESULT_COMPLETED, result.outcome)
        plan = load_json(result.output_dir / "replacement-plan.json")
        self.assertEqual("霓虹夜景", plan["primaryTargets"][0]["replacementValue"])
        self.assertEqual(STRATEGY_PER_IMAGE, plan["primaryTargets"][0]["decisionSource"])

    def test_supported_categories_each_complete_an_autonomous_end_to_end_result(self) -> None:
        scenarios = [
            {
                "itemId": "ordinary-person-route",
                "category": RULES["sourceCategories"]["ordinaryPerson"],
                "identity": "来源人物",
                "role": "画面中央的普通真人",
                "replacementValues": ["新生成的青年男性", "新生成的中年男性"],
                "title": "桌边的专注时刻",
                "subjectId": "main_person",
                "subjectRole": "subject",
                "suggestions": ["新生成的青年女性", "新生成的中年女性", "新生成的老年男性"],
            },
            {
                "itemId": "known-ip-route",
                "category": RULES["sourceCategories"]["knownCharacterIp"],
                "identity": "来源动画角色",
                "role": "画面中央的知名角色",
                "replacementValues": ["史努比", "加菲猫"],
                "title": "桌边的慵懒时刻",
                "subjectId": "main_character",
                "subjectRole": "subject",
                "suggestions": ["加菲猫", "唐老鸭", "姆明"],
            },
            {
                "itemId": "ordinary-object-route",
                "category": RULES["sourceCategories"]["genericObject"],
                "identity": "透明玻璃杯",
                "role": "画面中央的主要杯子",
                "replacementValues": ["陶瓷马克杯", "透明花瓶"],
                "title": "桌边倾斜的静物",
                "subjectId": "main_object",
                "subjectRole": "subject",
                "suggestions": ["透明花瓶", "金属水壶", "陶瓷碗"],
            },
            {
                "itemId": "text-content-route",
                "category": RULES["sourceCategories"]["textContent"],
                "identity": "来源主标题",
                "role": "画面中央的主要文字",
                "replacementValues": ["今天也要加油", "周末继续躺平"],
                "title": "醒目的中央文字",
                "subjectId": "main_text",
                "subjectRole": "text",
                "suggestions": ["周末继续躺平", "今天准时下班", "明天再继续"],
                "targetEligibility": {"textRewriteRequiredByMechanism": True},
            },
            {
                "itemId": "scene-attribute-route",
                "category": RULES["sourceCategories"]["sceneAttribute"],
                "identity": "阴天客厅",
                "role": "窗外环境与室内光线",
                "replacementValues": ["霓虹夜景", "雪天清晨"],
                "title": "窗边的静谧时刻",
                "subjectId": "main_scene",
                "subjectRole": "scene",
                "suggestions": ["雪天清晨", "雨夜黄昏", "暖色夕阳"],
                "targetEligibility": {
                    "primarySubjectHasReplacementValue": False,
                    "sceneChangeCreatesStableTemplateValue": True,
                },
            },
        ]

        for scenario in scenarios:
            with self.subTest(category=scenario["category"]):
                adapters = ScenarioAdapters(
                    source_scenario(
                        scenario["category"],
                        scenario["identity"],
                        scenario["role"],
                        scenario["replacementValues"],
                        scenario.get("targetEligibility"),
                    ),
                    approved_scenario(
                        title=scenario["title"],
                        subject_id=scenario["subjectId"],
                        subject_default=scenario["replacementValues"][0],
                        subject_suggestions=scenario["suggestions"],
                        subject_role=scenario["subjectRole"],
                    ),
                    identity_text_applicable=scenario["category"] in {
                        RULES["sourceCategories"]["ordinaryPerson"],
                        RULES["sourceCategories"]["knownCharacterIp"],
                    },
                )
                request = {**self.request, "productionItemId": scenario["itemId"]}

                result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

                self.assertEqual(RESULT_COMPLETED, result.outcome)
                plan = load_json(result.output_dir / "replacement-plan.json")
                self.assertEqual(scenario["category"], plan["primaryTargets"][0]["replacementCategory"])
                self.assertEqual(scenario["replacementValues"][0], plan["primaryTargets"][0]["replacementValue"])
                self.assertTrue(result.gallery_template.is_file())


if __name__ == "__main__":
    unittest.main()
