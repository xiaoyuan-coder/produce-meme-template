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
from tests.fixture_contracts import (
    author_explicit_slot_suggestion_reviews,
    rebuild_rendering_coherence_decision,
    rebuild_runtime_targets,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
SCENARIO_FIXTURE = ROOT / "fixtures" / "e2e" / "multi-instance"
RULES = json.loads((ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8"))
SCENARIOS = json.loads((SCENARIO_FIXTURE / "scenarios.json").read_text(encoding="utf-8"))
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")
CONTRACT = RULES["multiInstanceContract"]
GRAPH_FIELDS = CONTRACT["graphFields"]
COMPONENT_FIELDS = CONTRACT["componentFields"]
RELATION_FIELDS = CONTRACT["relationFields"]
OPERATION_FIELDS = CONTRACT["operationFields"]
OPERATION_REVIEW_FIELDS = CONTRACT["operationReviewFields"]
OPERATION_EVIDENCE_FIELD = RULES["visualReviewContract"]["evidenceFieldRoles"][
    "imageOperations"
]
COUNT_FIELDS = RULES["slotCompilationContract"]["assetUnitCountFields"]


def canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def component_graph(scenario: dict, *, approved: bool) -> dict:
    components = []
    source = scenario["approvedComponents" if approved else "sourceComponents"]
    for item in source:
        if approved:
            component_id, role, identity_id, visual, upload_id, control_id, container_id = item
        else:
            component_id, role, identity_id, visual, container_id = item
            upload_id = None
            control_id = None
        components.append(
            {
                COMPONENT_FIELDS["identity"]: component_id,
                COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"][role],
                COMPONENT_FIELDS["identityUnit"]: identity_id,
                COMPONENT_FIELDS["visualInstance"]: visual,
                COMPONENT_FIELDS["uploadAsset"]: upload_id,
                COMPONENT_FIELDS["control"]: control_id,
                COMPONENT_FIELDS["container"]: container_id,
                COMPONENT_FIELDS["explanation"]: f"逐区域确认组件 {component_id}",
            }
        )
    relations = [
        {
            RELATION_FIELDS["identity"]: relation_id,
            RELATION_FIELDS["type"]: CONTRACT["relationTypes"][relation_role],
            RELATION_FIELDS["source"]: source_id,
            RELATION_FIELDS["target"]: target_id,
            RELATION_FIELDS["explanation"]: f"逐区域确认关系 {relation_id}",
        }
        for relation_id, relation_role, source_id, target_id in scenario[
            "approvedRelations" if approved else "sourceRelations"
        ]
    ]
    return {
        GRAPH_FIELDS["components"]: components,
        GRAPH_FIELDS["relations"]: relations,
        GRAPH_FIELDS["explanation"]: "画面组件、身份、实例、容器与空间关系已分别清点",
    }


def image_operations(scenario: dict) -> list[dict]:
    return [
        {
            OPERATION_FIELDS["identity"]: f"operation-{scenario['operationRole']}",
            OPERATION_FIELDS["operation"]: CONTRACT["operations"][scenario["operationRole"]],
            OPERATION_FIELDS["targetRegions"]: scenario["targetComponents"],
            OPERATION_FIELDS["clearRequirements"]: ["清除目标区域的旧身份、旧内容和残留边缘"],
            OPERATION_FIELDS["stableAnchors"]: scenario["stableComponents"],
            OPERATION_FIELDS["preservedRelations"]: scenario["relationIdsToPreserve"],
            OPERATION_FIELDS["explanation"]: "根据单图机制选择图片操作并声明目标、锚点与关系",
        }
    ]


class MultiInstanceAdapters(DeterministicFixtureAdapters):
    def __init__(
        self,
        scenario: dict,
        *,
        source_mutator: Callable[[dict], None] | None = None,
        approved_mutator: Callable[[dict], None] | None = None,
        review_mutator: Callable[[dict], None] | None = None,
    ):
        super().__init__(BASE_FIXTURE)
        self.scenario = copy.deepcopy(scenario)
        self.source_mutator = source_mutator
        self.approved_mutator = approved_mutator
        self.review_mutator = review_mutator
        self.review_operation_ids: list[str] = []

    @property
    def scenario_fixture(self) -> Path:
        return SCENARIO_FIXTURE / self.scenario["fixtureDirectory"]

    def analyze_source(self, source_image: Path, replacement_strategy: dict | None) -> dict:
        analysis = super().analyze_source(source_image, replacement_strategy)
        category = RULES["sourceCategories"][self.scenario["sourceCategoryRole"]]
        analysis["mechanism"] = {
            **analysis["mechanism"],
            "setup": self.scenario["mechanismSetup"],
            "turn": "只改变图片操作指定的目标区域",
            "payoff": "操作后保持容器、顺序、接触与非目标内容",
        }
        analysis["target"] = {
            "category": category,
            "role": self.scenario["targetRole"],
            "identity": self.scenario["sourceIdentity"],
        }
        analysis["replacementPool"] = [
            {
                "value": self.scenario["replacementValue"],
                "category": category,
                "semanticCompatible": True,
                "visualCompatible": True,
                "rightsAndSafety": "pass",
                "score": 0.97,
                "reason": "与当前图片操作和空间机制相容",
            },
            {
                "value": "跨机制干扰项",
                "category": RULES["sourceCategories"]["genericFood"],
                "semanticCompatible": False,
                "visualCompatible": False,
                "rightsAndSafety": "pass",
                "score": 0.1,
                "reason": "不能承接当前图片操作的目标责任",
            },
        ]
        analysis["targetEligibility"] = (
            {
                "primarySubjectHasReplacementValue": False,
                "sceneChangeCreatesStableTemplateValue": True,
            }
            if self.scenario["sourceCategoryRole"] == "sceneAttribute"
            else {}
        )
        analysis["frozenSet"] = [
            "稳定锚点的位置与尺寸",
            "容器层级与面板顺序",
            "非目标内容和接触遮挡边界",
        ]
        analysis["forbiddenLegacyClaims"] = [self.scenario["sourceIdentity"]]
        graph = component_graph(self.scenario, approved=False)
        operations = image_operations(self.scenario)
        analysis[CONTRACT["sourceFields"]["componentGraph"]] = graph
        analysis[CONTRACT["sourceFields"]["imageOperations"]] = operations
        dependency_fields = RULES["identityReplacementContract"]["dependencyFields"]
        dependency_types = RULES["identityReplacementContract"]["dependencyTypes"]
        component_by_id = {
            component[COMPONENT_FIELDS["identity"]]: component
            for component in graph[GRAPH_FIELDS["components"]]
        }

        def dependency_type(index: int, component_id: str) -> str:
            if self.scenario["operationRole"] != "identityReplace":
                dependency_role = CONTRACT["operationDependencyTypes"][
                    self.scenario["operationRole"]
                ]
                return dependency_types[dependency_role]
            component_role = component_by_id[component_id][COMPONENT_FIELDS["role"]]
            if component_role == CONTRACT["componentRoles"]["reflection"]:
                return dependency_types["reflection"]
            if component_role == CONTRACT["componentRoles"]["shadow"]:
                return dependency_types["shadow"]
            return dependency_types["fullBody" if index == 0 else "repeatedInstance"]

        analysis["dependencyClosure"] = [
            {
                dependency_fields["componentIdentity"]: component_id,
                dependency_fields["dependencyType"]: dependency_type(index, component_id),
                dependency_fields["description"]: f"同步处理目标组件 {component_id}",
            }
            for index, component_id in enumerate(self.scenario["targetComponents"])
        ]
        analysis["spatialRelations"] = [
            relation[RELATION_FIELDS["explanation"]]
            for relation in graph[GRAPH_FIELDS["relations"]]
        ] or ["目标区域与稳定锚点保持原有空间层级"]
        if self.source_mutator:
            self.source_mutator(analysis)
        return analysis

    def generate(self, source_image: Path, generation_package: dict) -> dict:
        generated = super().generate(source_image, generation_package)
        approved_image = self.scenario_fixture / "approved.ppm"
        return {
            **generated,
            **self._fixture_image_result(approved_image),
        }

    def inspect_generated(self, generated_image: Path, review_context: dict) -> dict:
        review = super().inspect_generated(generated_image, review_context)
        operations = review_context[CONTRACT["generationFields"]["imageOperations"]]
        self.review_operation_ids.extend(
            operation[OPERATION_FIELDS["identity"]] for operation in operations
        )
        review[OPERATION_EVIDENCE_FIELD] = [
            {
                OPERATION_REVIEW_FIELDS["operationIdentity"]: operation[
                    OPERATION_FIELDS["identity"]
                ],
                OPERATION_REVIEW_FIELDS["targetCleared"]: True,
                OPERATION_REVIEW_FIELDS["anchorsStable"]: True,
                OPERATION_REVIEW_FIELDS["relationsPreserved"]: True,
                OPERATION_REVIEW_FIELDS["nonTargetStable"]: True,
                OPERATION_REVIEW_FIELDS["explanation"]: "逐区域核对目标清除、稳定锚点、接触遮挡和非目标内容",
            }
            for operation in operations
        ]
        if self.review_mutator:
            self.review_mutator(review)
        evidence_fields = RULES["visualReviewContract"]["evidenceFieldRoles"].values()
        review["bindings"]["evidenceSha256"] = canonical_sha(
            {field: review[field] for field in evidence_fields}
        )
        return review

    def analyze_approved(self, approved_image: Path) -> dict:
        analysis = super().analyze_approved(approved_image)
        analysis["visibleFacts"] = [
            self.scenario["mechanismSetup"],
            "目标区域、稳定锚点和容器边界可逐项识别",
            "图片操作后非目标区域保持稳定",
        ]
        analysis["neutralTitle"] = self.scenario["neutralTitle"]
        analysis["titleEvidence"] = {
            "templateGrounded": True,
            "usageMotivation": True,
            "spokenNaturalness": True,
            "slotPortability": True,
            "evidence": (
                f"标题只概括当前场景的可见机制："
                f"{self.scenario['mechanismSetup']}；替换所有开放内容后仍成立"
            ),
        }
        analysis["neutralDescription"] = self.scenario["neutralDescription"]
        subject_slot = analysis["slotCandidates"][0]
        scene_replacement = self.scenario["operationRole"] == "sceneReplace"
        ordered_content_group = self.scenario["operationRole"] == "orderedSet"
        content_image_replacement = self.scenario["operationRole"] == "maskFill"
        if content_image_replacement:
            subject_slot["type"] = RULES["slotCompilationContract"]["slotTypes"]["freePrompt"]
            subject_slot["semanticRole"] = RULES["slotCompilationContract"]["semanticRoles"]["supportingAppearance"]
            subject_slot["inputModes"] = ["text", "image"]
            subject_slot["imagePromptValue"] = "用户上传图中的完整手持物件"
            subject_slot["imageHint"] = "上传1张边界清晰的单个物件图片"
            subject_slot.pop("identityInheritanceDecision", None)
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    "userMotivation": False,
                    "visuallyVisible": True,
                    "modelControllable": True,
                    "mechanismPreserved": False,
                },
                "uploadReplacementFeasible": False,
                "blockerCode": "fixed_identity_is_mechanism_anchor",
                "evidence": "人物是握持机制的固定锚点，手持物以内容图片输入独立替换。",
            }
        if ordered_content_group:
            subject_slot["type"] = RULES["slotCompilationContract"]["slotTypes"]["freePrompt"]
            subject_slot["semanticRole"] = RULES["slotCompilationContract"]["semanticRoles"]["sceneContent"]
            subject_slot.pop("identityInheritanceDecision", None)
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    "userMotivation": True,
                    "visuallyVisible": True,
                    "modelControllable": True,
                    "mechanismPreserved": False,
                },
                "uploadReplacementFeasible": False,
                "blockerCode": "inseparable_multi_identity_unit",
                "evidence": "v2 将四个独立身份位作为有序内容组编辑，不合并为单主体上传。",
            }
        replacement_slot = analysis["slotCandidates"][2] if scene_replacement else subject_slot
        replacement_slot["defaultValue"] = self.scenario["replacementValue"]
        replacement_slot["suggestions"] = self.scenario["replacementSuggestions"]
        replacement_slot["titleForbiddenTokens"] = [
            self.scenario["replacementValue"],
            *self.scenario["replacementSuggestions"],
        ]
        container_default = analysis["slotCandidates"][1]["defaultValue"]
        scene_default = analysis["slotCandidates"][2]["defaultValue"]
        if scene_replacement:
            analysis["slotCandidates"] = analysis["slotCandidates"][1:]
            analysis["subjectSlotOmissionEvidence"] = {
                "reviewed": True,
                "valueGates": {
                    "userMotivation": False,
                    "visuallyVisible": True,
                    "modelControllable": True,
                    "mechanismPreserved": False,
                },
                "uploadReplacementFeasible": False,
                "blockerCode": "fixed_identity_is_mechanism_anchor",
                "evidence": "当前机制仅替换场景，主体作为稳定锚点不开放。",
            }
            analysis["promptTemplate"] = (
                f'保留{{{{ cushion_look | "{container_default}" }}}}的层次，'
                f'将背景替换为{{{{ room_mood | "{scene_default}" }}}}。'
                "边缘关系清楚，构图层级稳定。"
            )
        else:
            analysis["promptTemplate"] = (
                f'以{{{{ pet_subject | "{self.scenario["replacementValue"]}" }}}}作为核心内容，'
                f'保留{{{{ cushion_look | "{container_default}" }}}}，'
                f'并让{{{{ room_mood | "{scene_default}" }}}}统一画面。'
                "边缘关系清楚，构图层级稳定。"
            )
        analysis["freeEditableContent"] = ["边缘关系清楚", "构图层级稳定"]
        approved_graph = component_graph(self.scenario, approved=True)
        analysis[CONTRACT["approvedFields"]["componentGraph"]] = approved_graph
        if content_image_replacement:
            analysis["containerDependencies"] = [
                {
                    "contentInputId": "pet_subject",
                    "contentTargetId": "approved-held-object",
                    "containerTargetId": None,
                    "classification": "independent",
                    "preservedLayer": "双手与手持物的接触、遮挡和前后层级",
                    "evidence": "手持物与双手属于接触遮挡，不存在封闭容器轮廓。",
                }
            ]
        binding_fields = CONTRACT["approvedOperationBindingFields"]
        approved_component_by_id = {
            component[COMPONENT_FIELDS["identity"]]: component
            for component in approved_graph[GRAPH_FIELDS["components"]]
        }
        approved_target_ids = self.scenario["approvedTargetComponents"]
        analysis[CONTRACT["approvedFields"]["operationBindings"]] = [
            {
                binding_fields["operationIdentity"]: f"operation-{self.scenario['operationRole']}",
                binding_fields["targetComponents"]: approved_target_ids,
                binding_fields["stableAnchors"]: self.scenario[
                    "approvedStableComponents"
                ],
                binding_fields["controls"]: sorted(
                    {
                        approved_component_by_id[component_id][
                            COMPONENT_FIELDS["control"]
                        ]
                        for component_id in approved_target_ids
                        if approved_component_by_id[component_id][
                            COMPONENT_FIELDS["control"]
                        ]
                        is not None
                    }
                ),
                binding_fields["explanation"]: "逐项绑定确认模板图的操作目标、稳定锚点和实际控件",
            }
        ]
        visible, identities, uploads, controls = self.scenario["expectedCounts"]
        analysis["assetUnitAnalysis"].update(
            {
                COUNT_FIELDS["visibleSubjects"]: visible,
                COUNT_FIELDS["identities"]: identities,
                COUNT_FIELDS["uploads"]: uploads,
                COUNT_FIELDS["controls"]: controls,
                "evidence": "由确认模板图组件图分别计算四类数量",
            }
        )
        if self.approved_mutator:
            self.approved_mutator(analysis)
        analysis = rebuild_runtime_targets(analysis, RULES)
        return rebuild_rendering_coherence_decision(analysis, RULES)

    def audit_semantics(self, content: dict) -> dict:
        audit = super().audit_semantics(content)
        author_explicit_slot_suggestion_reviews(audit, content, RULES)
        content_sha = canonical_sha(content)
        audit["contentSha256"] = content_sha
        audit["observedContentSha256"] = content_sha
        return audit


class Issue9MultiInstanceOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = json.loads((BASE_FIXTURE / "request.json").read_text(encoding="utf-8"))
        self.request["sourceImage"] = str(BASE_FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_case(self, item_id: str, adapters: MultiInstanceAdapters):
        return run_production(
            {
                **self.request,
                "productionItemId": item_id,
                "sourceImage": str(adapters.scenario_fixture / "source.ppm"),
            },
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

    def test_five_image_operations_compile_through_the_public_workflow(self) -> None:
        source_hashes: set[str] = set()
        approved_hashes: set[str] = set()
        for name, scenario in SCENARIOS.items():
            with self.subTest(name=name):
                adapters = MultiInstanceAdapters(scenario)
                result = self.run_case(f"operation-{name.lower()}", adapters)
                self.assertEqual(RULES["resultStates"]["completed"], result.state)
                formal = json.loads(result.gallery_template.read_text())
                if name == "repeatedPet":
                    identity_binding = formal["runtimeSemantics"]["inputBindings"][
                        "pet_subject"
                    ]
                    self.assertEqual(
                        "same_source_repeated", identity_binding["bindingPolicy"]
                    )
                    self.assertGreaterEqual(len(identity_binding["targetIds"]), 2)
                if name == "personContactObject":
                    content_slot = next(
                        item
                        for item in formal["inputSchema"]["slots"]
                        if item["id"] == "pet_subject"
                    )
                    self.assertIn("text", content_slot)
                    self.assertIn("image", content_slot)
                    self.assertEqual(
                        "replace_content",
                        formal["runtimeSemantics"]["inputBindings"]["pet_subject"][
                            "operation"
                        ],
                    )
                    self.assertTrue(
                        any(
                            "不将它编译为其他目标的轮廓填充" in relation
                            for relation in formal["runtimeSemantics"]["visualContract"][
                                "relations"
                            ]
                        )
                    )
                plan = json.loads((result.output_dir / "replacement-plan.json").read_text())
                package = json.loads((result.output_dir / "generation-package.json").read_text())
                plan_operation = plan[CONTRACT["planFields"]["imageOperations"]][0]
                self.assertEqual(
                    CONTRACT["operations"][scenario["operationRole"]],
                    plan_operation[OPERATION_FIELDS["operation"]],
                )
                self.assertEqual(
                    plan[CONTRACT["planFields"]["imageOperations"]],
                    package[CONTRACT["generationFields"]["imageOperations"]],
                )
                self.assertEqual(
                    [plan_operation[OPERATION_FIELDS["identity"]]],
                    adapters.review_operation_ids,
                )
                self.assertEqual(1, len(adapters.upload_calls))
                source_hash = hashlib.sha256(
                    (adapters.scenario_fixture / "source.ppm").read_bytes()
                ).hexdigest()
                approved_hash = hashlib.sha256(
                    (adapters.scenario_fixture / "approved.ppm").read_bytes()
                ).hexdigest()
                self.assertNotEqual(source_hash, approved_hash)
                source_hashes.add(source_hash)
                approved_hashes.add(approved_hash)
        self.assertEqual(len(SCENARIOS), len(source_hashes))
        self.assertEqual(len(SCENARIOS), len(approved_hashes))

    def test_four_counts_are_derived_independently_from_the_approved_component_graph(self) -> None:
        for name in ("framedWholeImage", "multiPersonGrid", "repeatedPet", "personContactObject"):
            with self.subTest(name=name):
                scenario = SCENARIOS[name]
                result = self.run_case(f"counts-{name.lower()}", MultiInstanceAdapters(scenario))
                self.assertEqual(RULES["resultStates"]["completed"], result.state)
                editable = json.loads((result.output_dir / "editable-template-spec.json").read_text())
                expected = scenario["expectedCounts"]
                counts = editable["assetUnitAnalysis"]
                self.assertEqual(expected[0], counts[COUNT_FIELDS["visibleSubjects"]])
                self.assertEqual(expected[1], counts[COUNT_FIELDS["identities"]])
                self.assertEqual(expected[2], counts[COUNT_FIELDS["uploads"]])
                self.assertEqual(expected[3], counts[COUNT_FIELDS["controls"]])
                if name == "multiPersonGrid":
                    formal = json.loads(result.gallery_template.read_text())
                    group_input = next(
                        item for item in formal["inputSchema"]["slots"] if item["id"] == "pet_subject"
                    )
                    self.assertIn("text", group_input)
                    self.assertNotIn("image", group_input)
                    self.assertEqual(
                        "preserve_target_group",
                        formal["runtimeSemantics"]["inputBindings"]["pet_subject"][
                            "distributionPolicy"
                        ],
                    )

        def collapse_counts(analysis: dict) -> None:
            analysis["assetUnitAnalysis"][COUNT_FIELDS["visibleSubjects"]] = 1
            analysis["assetUnitAnalysis"][COUNT_FIELDS["identities"]] = 1
            analysis["assetUnitAnalysis"][COUNT_FIELDS["uploads"]] = 1

        adapters = MultiInstanceAdapters(
            SCENARIOS["multiPersonGrid"], approved_mutator=collapse_counts
        )
        result = self.run_case("collapsed-independent-counts", adapters)
        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual([], adapters.upload_calls)

    def test_operation_shape_and_contact_relation_coverage_block_before_generation(self) -> None:
        def move_contacts_to_unbound_source_anchor(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            component_id = "unbound-source-person"
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: component_id,
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["subject"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "未声明为稳定锚点的额外人物",
                }
            )
            for relation in graph[GRAPH_FIELDS["relations"]]:
                if relation[RELATION_FIELDS["type"]] in {
                    CONTRACT["relationTypes"]["contact"],
                    CONTRACT["relationTypes"]["occlusion"],
                }:
                    relation[RELATION_FIELDS["source"]] = component_id

        mutations = {
            "missing-target": lambda analysis: analysis[CONTRACT["sourceFields"]["imageOperations"]][0].update(
                {OPERATION_FIELDS["targetRegions"]: []}
            ),
            "unknown-anchor": lambda analysis: analysis[CONTRACT["sourceFields"]["imageOperations"]][0].update(
                {OPERATION_FIELDS["stableAnchors"]: ["not-a-component"]}
            ),
            "missing-contact": lambda analysis: analysis[CONTRACT["sourceFields"]["imageOperations"]][0].update(
                {OPERATION_FIELDS["preservedRelations"]: []}
            ),
            "relation-outside-operation-scope": move_contacts_to_unbound_source_anchor,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                adapters = MultiInstanceAdapters(
                    SCENARIOS["personContactObject"], source_mutator=mutate
                )
                result = self.run_case(f"invalid-operation-{name}", adapters)
                self.assertIn(
                    result.state,
                    {RULES["resultStates"]["failed"], RULES["resultStates"]["blocked"]},
                )
                self.assertEqual([], adapters.generate_calls)
                self.assertEqual([], adapters.upload_calls)

    def test_identity_operation_must_cover_repeated_instances_and_derivatives(self) -> None:
        def omit_repeat(analysis: dict) -> None:
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["targetRegions"]].remove("pet-repeat-a")

        adapters = MultiInstanceAdapters(SCENARIOS["repeatedPet"], source_mutator=omit_repeat)
        result = self.run_case("identity-operation-omits-repeat", adapters)
        self.assertIn(
            result.state,
            {RULES["resultStates"]["failed"], RULES["resultStates"]["blocked"]},
        )
        self.assertEqual([], adapters.generate_calls)

    def test_operation_targets_must_exactly_cover_the_named_dependency_closure(self) -> None:
        dependency_fields = RULES["identityReplacementContract"]["dependencyFields"]

        def omit_named_dependency(analysis: dict) -> None:
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            omitted_target = operation[OPERATION_FIELDS["targetRegions"]][-1]
            analysis["dependencyClosure"] = [
                item
                for item in analysis["dependencyClosure"]
                if item[dependency_fields["componentIdentity"]] != omitted_target
            ]

        adapters = MultiInstanceAdapters(
            SCENARIOS["multiPersonGrid"], source_mutator=omit_named_dependency
        )
        result = self.run_case("operation-closure-mismatch", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

        def erase_names_and_omit_target(analysis: dict) -> None:
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["targetRegions"]].pop()
            for item in analysis["dependencyClosure"]:
                item.pop(dependency_fields["componentIdentity"])

        adapters = MultiInstanceAdapters(
            SCENARIOS["multiPersonGrid"], source_mutator=erase_names_and_omit_target
        )
        result = self.run_case("unnamed-operation-closure", adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_identity_operation_covers_every_component_in_the_identity_unit(self) -> None:
        def add_uncovered_nested_copy(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "pet-framed-copy",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["nestedContent"],
                    COMPONENT_FIELDS["identityUnit"]: "same-pet",
                    COMPONENT_FIELDS["visualInstance"]: True,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: "shared-cushion",
                    COMPONENT_FIELDS["explanation"]: "相框内仍是同一宠物身份的派生副本",
                }
            )
            graph[GRAPH_FIELDS["relations"]].append(
                {
                    RELATION_FIELDS["identity"]: "pet-copy-inside-frame",
                    RELATION_FIELDS["type"]: CONTRACT["relationTypes"]["nestedIn"],
                    RELATION_FIELDS["source"]: "pet-framed-copy",
                    RELATION_FIELDS["target"]: "shared-cushion",
                    RELATION_FIELDS["explanation"]: "同身份副本嵌在稳定容器中",
                }
            )

        adapters = MultiInstanceAdapters(
            SCENARIOS["repeatedPet"], source_mutator=add_uncovered_nested_copy
        )
        result = self.run_case("identity-operation-omits-nested-copy", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

        def split_reflection_identity(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            reflected = next(
                component
                for component in graph[GRAPH_FIELDS["components"]]
                if component[COMPONENT_FIELDS["identity"]] == "pet-repeat-b"
            )
            reflected[COMPONENT_FIELDS["identityUnit"]] = "different-pet"

        adapters = MultiInstanceAdapters(
            SCENARIOS["repeatedPet"], source_mutator=split_reflection_identity
        )
        result = self.run_case("reflection-relation-splits-identity", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)

    def test_operation_types_enforce_their_component_and_relation_semantics(self) -> None:
        dependency_fields = RULES["identityReplacementContract"]["dependencyFields"]

        def relabel_identity_as_scene(analysis: dict) -> None:
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["operation"]] = CONTRACT["operations"]["sceneReplace"]

        def relabel_identity_as_mask(analysis: dict) -> None:
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["operation"]] = CONTRACT["operations"]["maskFill"]

        def drop_order_relations(analysis: dict) -> None:
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["preservedRelations"]] = []

        def break_order_chain(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            retained = {"person-a-in-panel", "person-b-in-panel", "person-c-in-panel", "person-d-in-panel", "panel-a-before-b"}
            graph[GRAPH_FIELDS["relations"]] = [
                relation
                for relation in graph[GRAPH_FIELDS["relations"]]
                if relation[RELATION_FIELDS["identity"]] in retained
            ]
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["preservedRelations"]] = ["panel-a-before-b"]

        def add_subject_to_scene_targets(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "scene-stable-subject",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["subject"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: True,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "另一个保持稳定的主体锚点",
                }
            )
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["targetRegions"]].append("scene-subject")
            operation[OPERATION_FIELDS["stableAnchors"]] = ["scene-stable-subject"]
            analysis["dependencyClosure"].append(
                {
                    dependency_fields["componentIdentity"]: "scene-subject",
                    dependency_fields["dependencyType"]: RULES[
                        "identityReplacementContract"
                    ]["dependencyTypes"][CONTRACT["operationDependencyTypes"]["sceneReplace"]],
                    dependency_fields["description"]: "错误把稳定主体纳入场景替换",
                }
            )

        def add_uncovered_ordered_reflection(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "person-a-reflection",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["reflection"],
                    COMPONENT_FIELDS["identityUnit"]: "source-person-a",
                    COMPONENT_FIELDS["visualInstance"]: True,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: "grid-panel-a",
                    COMPONENT_FIELDS["explanation"]: "面板中人物 A 的同身份反射副本",
                }
            )
            graph[GRAPH_FIELDS["relations"]].append(
                {
                    RELATION_FIELDS["identity"]: "person-a-reflected",
                    RELATION_FIELDS["type"]: CONTRACT["relationTypes"]["reflection"],
                    RELATION_FIELDS["source"]: "person-a-reflection",
                    RELATION_FIELDS["target"]: "person-a",
                    RELATION_FIELDS["explanation"]: "反射副本与人物 A 属于同一身份单元",
                }
            )

        def use_unknown_dependency_type(analysis: dict) -> None:
            analysis["dependencyClosure"][0][dependency_fields["dependencyType"]] = "unknown_dependency_kind"

        def use_non_identity_machine_dependency_type(analysis: dict) -> None:
            dependency_role = CONTRACT["operationDependencyTypes"]["sceneReplace"]
            analysis["dependencyClosure"][0][dependency_fields["dependencyType"]] = RULES[
                "identityReplacementContract"
            ]["dependencyTypes"][dependency_role]

        cases = {
            "identity-as-scene": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"], source_mutator=relabel_identity_as_scene
            ),
            "identity-as-mask": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"], source_mutator=relabel_identity_as_mask
            ),
            "ordered-without-order": MultiInstanceAdapters(
                SCENARIOS["multiPersonGrid"], source_mutator=drop_order_relations
            ),
            "ordered-broken-chain": MultiInstanceAdapters(
                SCENARIOS["multiPersonGrid"], source_mutator=break_order_chain
            ),
            "ordered-omits-same-identity-reflection": MultiInstanceAdapters(
                SCENARIOS["multiPersonGrid"], source_mutator=add_uncovered_ordered_reflection
            ),
            "scene-targets-subject": MultiInstanceAdapters(
                SCENARIOS["sceneReplacement"], source_mutator=add_subject_to_scene_targets
            ),
            "unknown-dependency-type": MultiInstanceAdapters(
                SCENARIOS["personContactObject"], source_mutator=use_unknown_dependency_type
            ),
            "identity-with-scene-dependency-type": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"],
                source_mutator=use_non_identity_machine_dependency_type,
            ),
        }
        for name, adapters in cases.items():
            with self.subTest(name=name):
                result = self.run_case(f"invalid-operation-semantics-{name}", adapters)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertEqual([], adapters.generate_calls)

    def test_operation_visual_failures_block_approval_and_upload(self) -> None:
        for field_role in (
            "targetCleared",
            "anchorsStable",
            "relationsPreserved",
            "nonTargetStable",
        ):
            with self.subTest(field_role=field_role):
                def fail_review(review: dict, field_role: str = field_role) -> None:
                    review[OPERATION_EVIDENCE_FIELD][0][
                        OPERATION_REVIEW_FIELDS[field_role]
                    ] = False

                adapters = MultiInstanceAdapters(
                    SCENARIOS["personContactObject"], review_mutator=fail_review
                )
                result = self.run_case(f"operation-review-{field_role.lower()}", adapters)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["visualHardFailure"], result.error_code)
                self.assertEqual([], adapters.upload_calls)

    def test_nested_component_and_review_shapes_return_stable_results(self) -> None:
        def malformed_source_role(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]][0][COMPONENT_FIELDS["role"]] = {}

        def malformed_review_identity(review: dict) -> None:
            review[OPERATION_EVIDENCE_FIELD][0][
                OPERATION_REVIEW_FIELDS["operationIdentity"]
            ] = {}

        def malformed_approved_relation(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["relations"]][0][RELATION_FIELDS["type"]] = {}

        cases = {
            "source-role": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"], source_mutator=malformed_source_role
            ),
            "review-operation-id": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"], review_mutator=malformed_review_identity
            ),
            "approved-relation": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"], approved_mutator=malformed_approved_relation
            ),
        }
        for name, adapters in cases.items():
            with self.subTest(name=name):
                result = self.run_case(f"malformed-multi-instance-{name}", adapters)
                self.assertIn(
                    result.state,
                    {RULES["resultStates"]["failed"], RULES["resultStates"]["blocked"]},
                )
                self.assertEqual([], adapters.upload_calls)

    def test_approved_component_graph_rejects_unknown_control_and_broken_container(self) -> None:
        def create_container_cycle(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            first, second = graph[GRAPH_FIELDS["components"]][:2]
            first[COMPONENT_FIELDS["container"]] = second[COMPONENT_FIELDS["identity"]]
            second[COMPONENT_FIELDS["container"]] = first[COMPONENT_FIELDS["identity"]]

        def remove_subject_upload_unit(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            for component in graph[GRAPH_FIELDS["components"]]:
                if component[COMPONENT_FIELDS["control"]] == "pet_subject":
                    component[COMPONENT_FIELDS["uploadAsset"]] = None
            analysis["assetUnitAnalysis"][COUNT_FIELDS["uploads"]] = 0

        def move_subject_control_to_background(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            components = graph[GRAPH_FIELDS["components"]]
            subject = next(
                component
                for component in components
                if component[COMPONENT_FIELDS["identity"]] == "approved-pet-main"
            )
            background = next(
                component
                for component in components
                if component[COMPONENT_FIELDS["role"]]
                == CONTRACT["componentRoles"]["background"]
            )
            upload_id = subject[COMPONENT_FIELDS["uploadAsset"]]
            subject[COMPONENT_FIELDS["uploadAsset"]] = None
            subject[COMPONENT_FIELDS["control"]] = "room_mood"
            background[COMPONENT_FIELDS["uploadAsset"]] = upload_id
            background[COMPONENT_FIELDS["control"]] = "pet_subject"

        def split_approved_reflection_identity(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            reflected = next(
                component
                for component in graph[GRAPH_FIELDS["components"]]
                if component[COMPONENT_FIELDS["identity"]] == "approved-pet-repeat-b"
            )
            reflected[COMPONENT_FIELDS["identityUnit"]] = "other-approved-pet"
            analysis["assetUnitAnalysis"][COUNT_FIELDS["identities"]] = 2

        def break_approved_order_chain(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["relations"]] = graph[GRAPH_FIELDS["relations"]][:1]

        def erase_approved_relations(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["relations"]] = []

        def relabel_approved_reflection_as_contact(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            reflected = next(
                relation
                for relation in graph[GRAPH_FIELDS["relations"]]
                if relation[RELATION_FIELDS["identity"]] == "approved-repeat-b"
            )
            reflected[RELATION_FIELDS["type"]] = CONTRACT["relationTypes"]["contact"]

        def remove_approved_reflection_component(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]] = [
                component
                for component in graph[GRAPH_FIELDS["components"]]
                if component[COMPONENT_FIELDS["identity"]] != "approved-pet-repeat-b"
            ]
            graph[GRAPH_FIELDS["relations"]] = [
                relation
                for relation in graph[GRAPH_FIELDS["relations"]]
                if "approved-pet-repeat-b"
                not in {
                    relation[RELATION_FIELDS["source"]],
                    relation[RELATION_FIELDS["target"]],
                }
            ]
            analysis["assetUnitAnalysis"][COUNT_FIELDS["visibleSubjects"]] = 2

        mutations = {
            "unknown-control": lambda analysis: analysis[CONTRACT["approvedFields"]["componentGraph"]][GRAPH_FIELDS["components"]][0].update(
                {COMPONENT_FIELDS["control"]: "missing-control"}
            ),
            "broken-container": lambda analysis: analysis[CONTRACT["approvedFields"]["componentGraph"]][GRAPH_FIELDS["components"]][0].update(
                {COMPONENT_FIELDS["container"]: "missing-container"}
            ),
            "container-cycle": create_container_cycle,
            "subject-without-upload-unit": remove_subject_upload_unit,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                adapters = MultiInstanceAdapters(
                    SCENARIOS["framedWholeImage"], approved_mutator=mutate
                )
                result = self.run_case(f"approved-graph-{name}", adapters)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual([], adapters.upload_calls)

        topology_cases = {
            "control-role-mismatch": (
                SCENARIOS["repeatedPet"],
                move_subject_control_to_background,
            ),
            "split-reflection-identity": (
                SCENARIOS["repeatedPet"],
                split_approved_reflection_identity,
            ),
            "broken-approved-order-chain": (
                SCENARIOS["multiPersonGrid"],
                break_approved_order_chain,
            ),
            "missing-contact-relations": (
                SCENARIOS["personContactObject"],
                erase_approved_relations,
            ),
            "missing-nested-relation": (
                SCENARIOS["framedWholeImage"],
                erase_approved_relations,
            ),
            "reflection-relabelled-as-contact": (
                SCENARIOS["repeatedPet"],
                relabel_approved_reflection_as_contact,
            ),
            "missing-approved-reflection-component": (
                SCENARIOS["repeatedPet"],
                remove_approved_reflection_component,
            ),
        }
        for name, (scenario, mutate) in topology_cases.items():
            with self.subTest(name=name):
                adapters = MultiInstanceAdapters(
                    scenario, approved_mutator=mutate
                )
                result = self.run_case(f"approved-graph-{name}", adapters)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertEqual([], adapters.upload_calls)

    def test_approved_operation_targets_cannot_be_satisfied_by_unrelated_decoys(self) -> None:
        def split_identity_with_decoy_subject(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            components = graph[GRAPH_FIELDS["components"]]
            relations = graph[GRAPH_FIELDS["relations"]]
            decoy_id = "approved-decoy-subject"
            components.append(
                {
                    COMPONENT_FIELDS["identity"]: decoy_id,
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["subject"],
                    COMPONENT_FIELDS["identityUnit"]: "decoy-pet",
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "无关主体只用于伪造另一组身份关系",
                }
            )
            for component_id in ("approved-pet-repeat-b", "approved-pet-shadow"):
                component = next(
                    item
                    for item in components
                    if item[COMPONENT_FIELDS["identity"]] == component_id
                )
                component[COMPONENT_FIELDS["identityUnit"]] = "decoy-pet"
            for relation_id in ("approved-repeat-b", "approved-shadow"):
                relation = next(
                    item
                    for item in relations
                    if item[RELATION_FIELDS["identity"]] == relation_id
                )
                relation[RELATION_FIELDS["target"]] = decoy_id
            analysis["assetUnitAnalysis"][COUNT_FIELDS["identities"]] = 2

        def move_contact_relations_to_decoys(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            components = graph[GRAPH_FIELDS["components"]]
            relations = graph[GRAPH_FIELDS["relations"]]
            for component_id, role in (
                ("approved-decoy-person", "subject"),
                ("approved-decoy-prop", "prop"),
            ):
                components.append(
                    {
                        COMPONENT_FIELDS["identity"]: component_id,
                        COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"][role],
                        COMPONENT_FIELDS["identityUnit"]: None,
                        COMPONENT_FIELDS["visualInstance"]: False,
                        COMPONENT_FIELDS["uploadAsset"]: None,
                        COMPONENT_FIELDS["control"]: None,
                        COMPONENT_FIELDS["container"]: None,
                        COMPONENT_FIELDS["explanation"]: "无关组件只用于伪造全图关系类型覆盖",
                    }
                )
            for relation in relations:
                if relation[RELATION_FIELDS["type"]] in {
                    CONTRACT["relationTypes"]["contact"],
                    CONTRACT["relationTypes"]["occlusion"],
                }:
                    relation[RELATION_FIELDS["source"]] = "approved-decoy-person"
                    relation[RELATION_FIELDS["target"]] = "approved-decoy-prop"

        def move_contact_control_and_relations_to_decoys(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            components = graph[GRAPH_FIELDS["components"]]
            actual = next(
                component
                for component in components
                if component[COMPONENT_FIELDS["identity"]] == "approved-held-object"
            )
            upload_id = actual[COMPONENT_FIELDS["uploadAsset"]]
            control_id = actual[COMPONENT_FIELDS["control"]]
            actual[COMPONENT_FIELDS["uploadAsset"]] = None
            actual[COMPONENT_FIELDS["control"]] = None
            for component_id, role, upload, control in (
                ("approved-decoy-person", "subject", None, None),
                ("approved-decoy-prop", "prop", upload_id, control_id),
            ):
                components.append(
                    {
                        COMPONENT_FIELDS["identity"]: component_id,
                        COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"][role],
                        COMPONENT_FIELDS["identityUnit"]: None,
                        COMPONENT_FIELDS["visualInstance"]: False,
                        COMPONENT_FIELDS["uploadAsset"]: upload,
                        COMPONENT_FIELDS["control"]: control,
                        COMPONENT_FIELDS["container"]: None,
                        COMPONENT_FIELDS["explanation"]: "同角色 decoy 窃取实际目标的上传控件",
                    }
                )
            for relation in graph[GRAPH_FIELDS["relations"]]:
                if relation[RELATION_FIELDS["type"]] in {
                    CONTRACT["relationTypes"]["contact"],
                    CONTRACT["relationTypes"]["occlusion"],
                }:
                    relation[RELATION_FIELDS["source"]] = "approved-decoy-person"
                    relation[RELATION_FIELDS["target"]] = "approved-decoy-prop"

        def move_scene_control_to_decoy(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            components = graph[GRAPH_FIELDS["components"]]
            actual = next(
                component
                for component in components
                if component[COMPONENT_FIELDS["identity"]]
                == "approved-scene-background"
            )
            control_id = actual[COMPONENT_FIELDS["control"]]
            actual[COMPONENT_FIELDS["control"]] = None
            components.append(
                {
                    COMPONENT_FIELDS["identity"]: "approved-decoy-background",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["background"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: control_id,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "同角色 decoy 窃取实际场景目标的控件",
                }
            )

        def share_contact_control_with_unbound_decoy(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            actual = next(
                component
                for component in graph[GRAPH_FIELDS["components"]]
                if component[COMPONENT_FIELDS["identity"]] == "approved-held-object"
            )
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "approved-unbound-prop",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["prop"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: actual[
                        COMPONENT_FIELDS["uploadAsset"]
                    ],
                    COMPONENT_FIELDS["control"]: actual[COMPONENT_FIELDS["control"]],
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "未绑定道具与实际目标共享同一控件",
                }
            )

        def share_scene_control_with_unbound_decoy(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            actual = next(
                component
                for component in graph[GRAPH_FIELDS["components"]]
                if component[COMPONENT_FIELDS["identity"]]
                == "approved-scene-background"
            )
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "approved-unbound-background",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["background"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: actual[COMPONENT_FIELDS["control"]],
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "未绑定背景与实际场景目标共享同一控件",
                }
            )

        def add_unbound_same_identity_reflection(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "approved-unbound-reflection",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["reflection"],
                    COMPONENT_FIELDS["identityUnit"]: "new-same-pet",
                    COMPONENT_FIELDS["visualInstance"]: True,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "未绑定反射仍属于当前替换身份",
                }
            )
            graph[GRAPH_FIELDS["relations"]].append(
                {
                    RELATION_FIELDS["identity"]: "approved-unbound-reflection-relation",
                    RELATION_FIELDS["type"]: CONTRACT["relationTypes"]["reflection"],
                    RELATION_FIELDS["source"]: "approved-unbound-reflection",
                    RELATION_FIELDS["target"]: "approved-pet-main",
                    RELATION_FIELDS["explanation"]: "新增反射与已替换主体属于同一身份",
                }
            )
            analysis["assetUnitAnalysis"][COUNT_FIELDS["visibleSubjects"]] = 4

        def redirect_target_relations_to_unbound_anchor(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "approved-unbound-person-anchor",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["subject"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "未绑定人物不能代替 operation 声明的稳定锚点",
                }
            )
            for relation in graph[GRAPH_FIELDS["relations"]]:
                if relation[RELATION_FIELDS["type"]] in {
                    CONTRACT["relationTypes"]["contact"],
                    CONTRACT["relationTypes"]["occlusion"],
                }:
                    relation[RELATION_FIELDS["source"]] = (
                        "approved-unbound-person-anchor"
                    )

        def replace_ordered_anchors_with_decoy_panels(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            decoy_ids = []
            for index in range(4):
                component_id = f"approved-decoy-panel-{index}"
                decoy_ids.append(component_id)
                graph[GRAPH_FIELDS["components"]].append(
                    {
                        COMPONENT_FIELDS["identity"]: component_id,
                        COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["panel"],
                        COMPONENT_FIELDS["identityUnit"]: None,
                        COMPONENT_FIELDS["visualInstance"]: False,
                        COMPONENT_FIELDS["uploadAsset"]: None,
                        COMPONENT_FIELDS["control"]: None,
                        COMPONENT_FIELDS["container"]: None,
                        COMPONENT_FIELDS["explanation"]: "无关面板不能代替目标人物的实际容器",
                    }
                )
            binding_fields = CONTRACT["approvedOperationBindingFields"]
            binding = analysis[CONTRACT["approvedFields"]["operationBindings"]][0]
            binding[binding_fields["stableAnchors"]] = decoy_ids

        cases = {
            "split-identity-decoy": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"],
                approved_mutator=split_identity_with_decoy_subject,
            ),
            "contact-relations-on-decoys": MultiInstanceAdapters(
                SCENARIOS["personContactObject"],
                approved_mutator=move_contact_relations_to_decoys,
            ),
            "contact-control-on-decoy": MultiInstanceAdapters(
                SCENARIOS["personContactObject"],
                approved_mutator=move_contact_control_and_relations_to_decoys,
            ),
            "scene-control-on-decoy": MultiInstanceAdapters(
                SCENARIOS["sceneReplacement"],
                approved_mutator=move_scene_control_to_decoy,
            ),
            "contact-control-shared-with-decoy": MultiInstanceAdapters(
                SCENARIOS["personContactObject"],
                approved_mutator=share_contact_control_with_unbound_decoy,
            ),
            "scene-control-shared-with-decoy": MultiInstanceAdapters(
                SCENARIOS["sceneReplacement"],
                approved_mutator=share_scene_control_with_unbound_decoy,
            ),
            "same-identity-reflection-outside-binding": MultiInstanceAdapters(
                SCENARIOS["repeatedPet"],
                approved_mutator=add_unbound_same_identity_reflection,
            ),
            "relations-use-unbound-anchor": MultiInstanceAdapters(
                SCENARIOS["personContactObject"],
                approved_mutator=redirect_target_relations_to_unbound_anchor,
            ),
            "ordered-binding-uses-decoy-panels": MultiInstanceAdapters(
                SCENARIOS["multiPersonGrid"],
                approved_mutator=replace_ordered_anchors_with_decoy_panels,
            ),
        }
        for name, adapters in cases.items():
            with self.subTest(name=name):
                result = self.run_case(f"approved-operation-{name}", adapters)
                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
                self.assertEqual([], adapters.upload_calls)

    def test_each_preserved_source_relation_requires_a_bound_approved_counterpart(self) -> None:
        def add_second_contact_boundary(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            relation_id = "hand-holds-object-second-boundary"
            graph[GRAPH_FIELDS["relations"]].append(
                {
                    RELATION_FIELDS["identity"]: relation_id,
                    RELATION_FIELDS["type"]: CONTRACT["relationTypes"]["contact"],
                    RELATION_FIELDS["source"]: "person-main",
                    RELATION_FIELDS["target"]: "held-object",
                    RELATION_FIELDS["explanation"]: "第二处手指与物体的独立接触边界",
                }
            )
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["preservedRelations"]].append(relation_id)

        adapters = MultiInstanceAdapters(
            SCENARIOS["personContactObject"],
            source_mutator=add_second_contact_boundary,
        )
        result = self.run_case("approved-operation-relation-cardinality", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

        def add_second_subject_anchor_and_contact(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: "person-secondary-anchor",
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["subject"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "第二个独立人物稳定锚点",
                }
            )
            relation_id = "secondary-person-holds-object"
            graph[GRAPH_FIELDS["relations"]].append(
                {
                    RELATION_FIELDS["identity"]: relation_id,
                    RELATION_FIELDS["type"]: CONTRACT["relationTypes"]["contact"],
                    RELATION_FIELDS["source"]: "person-secondary-anchor",
                    RELATION_FIELDS["target"]: "held-object",
                    RELATION_FIELDS["explanation"]: "第二人物与目标道具形成独立接触",
                }
            )
            operation = analysis[CONTRACT["sourceFields"]["imageOperations"]][0]
            operation[OPERATION_FIELDS["stableAnchors"]].append(
                "person-secondary-anchor"
            )
            operation[OPERATION_FIELDS["preservedRelations"]].append(relation_id)

        def collapse_two_contacts_onto_first_approved_anchor(analysis: dict) -> None:
            graph = analysis[CONTRACT["approvedFields"]["componentGraph"]]
            component_id = "approved-secondary-person-anchor"
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: component_id,
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["subject"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "第二个 Approved 人物锚点",
                }
            )
            graph[GRAPH_FIELDS["relations"]].append(
                {
                    RELATION_FIELDS["identity"]: "approved-collapsed-second-contact",
                    RELATION_FIELDS["type"]: CONTRACT["relationTypes"]["contact"],
                    RELATION_FIELDS["source"]: "approved-person",
                    RELATION_FIELDS["target"]: "approved-held-object",
                    RELATION_FIELDS["explanation"]: "错误把第二接触也折叠到第一人物锚点",
                }
            )
            binding_fields = CONTRACT["approvedOperationBindingFields"]
            binding = analysis[CONTRACT["approvedFields"]["operationBindings"]][0]
            binding[binding_fields["stableAnchors"]].append(component_id)

        adapters = MultiInstanceAdapters(
            SCENARIOS["personContactObject"],
            source_mutator=add_second_subject_anchor_and_contact,
            approved_mutator=collapse_two_contacts_onto_first_approved_anchor,
        )
        result = self.run_case("approved-operation-relation-endpoint-mapping", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_approved_target_ownership_is_unique_across_operations(self) -> None:
        dependency_fields = RULES["identityReplacementContract"]["dependencyFields"]

        def add_second_scene_operation(analysis: dict) -> None:
            graph = analysis[CONTRACT["sourceFields"]["componentGraph"]]
            component_id = "scene-background-secondary"
            graph[GRAPH_FIELDS["components"]].append(
                {
                    COMPONENT_FIELDS["identity"]: component_id,
                    COMPONENT_FIELDS["role"]: CONTRACT["componentRoles"]["background"],
                    COMPONENT_FIELDS["identityUnit"]: None,
                    COMPONENT_FIELDS["visualInstance"]: False,
                    COMPONENT_FIELDS["uploadAsset"]: None,
                    COMPONENT_FIELDS["control"]: None,
                    COMPONENT_FIELDS["container"]: None,
                    COMPONENT_FIELDS["explanation"]: "第二个独立场景替换目标",
                }
            )
            second_operation_id = "operation-sceneReplace-secondary"
            analysis[CONTRACT["sourceFields"]["imageOperations"]].append(
                {
                    OPERATION_FIELDS["identity"]: second_operation_id,
                    OPERATION_FIELDS["operation"]: CONTRACT["operations"][
                        "sceneReplace"
                    ],
                    OPERATION_FIELDS["targetRegions"]: [component_id],
                    OPERATION_FIELDS["clearRequirements"]: ["清除第二场景区域旧内容"],
                    OPERATION_FIELDS["stableAnchors"]: ["scene-subject"],
                    OPERATION_FIELDS["preservedRelations"]: [],
                    OPERATION_FIELDS["explanation"]: "第二场景区域独立替换",
                }
            )
            analysis["dependencyClosure"].append(
                {
                    dependency_fields["componentIdentity"]: component_id,
                    dependency_fields["dependencyType"]: RULES[
                        "identityReplacementContract"
                    ]["dependencyTypes"][CONTRACT["operationDependencyTypes"]["sceneReplace"]],
                    dependency_fields["description"]: "同步处理第二场景目标",
                }
            )

        def bind_both_operations_to_one_background(analysis: dict) -> None:
            binding_fields = CONTRACT["approvedOperationBindingFields"]
            first = analysis[CONTRACT["approvedFields"]["operationBindings"]][0]
            analysis[CONTRACT["approvedFields"]["operationBindings"]].append(
                {
                    **copy.deepcopy(first),
                    binding_fields[
                        "operationIdentity"
                    ]: "operation-sceneReplace-secondary",
                    binding_fields["explanation"]: "错误将第二操作继续绑到第一背景",
                }
            )

        adapters = MultiInstanceAdapters(
            SCENARIOS["sceneReplacement"],
            source_mutator=add_second_scene_operation,
            approved_mutator=bind_both_operations_to_one_background,
        )
        result = self.run_case("approved-target-owned-by-two-operations", adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)


if __name__ == "__main__":
    unittest.main()
