from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.produce_meme_template import (
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
    run_production,
)
from scripts.produce_meme_template.workflow_core import (
    validate_production_manifest_lineage,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Issue17MajorStagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(
            FIXTURE / self.request["sourceImage"]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_stage(self, stage: int | str, adapters):
        return run_production(
            self.request,
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=stage,
        )

    def test_stage_one_freezes_product_print_and_screenshot_canvas_routing_in_prompt(
        self,
    ) -> None:
        scenarios = {
            "shirt-print": {
                "mode": "print_artwork",
                "targetRegionIds": ["shirt-print-region"],
                "excludedCarrierRegionIds": ["shirt-body", "model-body"],
                "requiredActions": ["extract_print_artwork", "rectify_perspective"],
                "preserveDesignFeatures": ["印花内部文字层级", "拼贴阅读顺序"],
                "evidence": "图案位于衣服表面，离开载体后仍可独立阅读",
                "expected": ("只提取衣服表面的独立印花", "正视化", "衣服本体", "模特身体"),
            },
            "black-frame-screenshot": {
                "mode": "screen_content",
                "targetRegionIds": ["screen-content-region"],
                "excludedCarrierRegionIds": ["black-screen-frame", "device-ui"],
                "requiredActions": ["crop_interface_frame"],
                "preserveDesignFeatures": ["截图内容区的版式", "内容区阅读顺序"],
                "evidence": "来源是带黑色截屏框和设备界面的内容截图",
                "expected": ("只保留截图内容区", "裁掉黑色截屏框", "设备边框", "界面控件"),
            },
        }

        for item_id, scenario in scenarios.items():
            with self.subTest(item_id=item_id):
                class CanvasRoutingAdapters(DeterministicFixtureAdapters):
                    def analyze_source(self, source_image, replacement_strategy):
                        analysis = super().analyze_source(
                            source_image, replacement_strategy
                        )
                        analysis["sourceCanvasDecision"] = {
                            key: value
                            for key, value in scenario.items()
                            if key != "expected"
                        }
                        return analysis

                result = run_production(
                    {
                        **self.request,
                        "productionItemId": f"source-canvas-{item_id}",
                    },
                    self.output_root,
                    CanvasRoutingAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                    stage=1,
                )

                self.assertEqual("completed", result.outcome)
                package = load_json(result.output_dir / "generation-package.json")
                for wording in scenario["expected"]:
                    self.assertIn(wording, package["prompt"])
                gate = package["firstStageGate"]
                self.assertTrue(gate["sourceCanvasComplete"])
                requirement_id = RULES["criticalOutcomeContract"]["requirementIds"][
                    "sourceCanvasNormalization"
                ]
                self.assertIn(
                    requirement_id,
                    {
                        item["requirementId"]
                        for item in gate["requirementResults"]
                        if item["pass"] is True
                    },
                )

    def test_stage_one_blocks_missing_canvas_routing_and_illegal_sticker_removal(
        self,
    ) -> None:
        canvas_field = RULES["sourceCanvasContract"]["field"]
        mark_contract = RULES["sourceMarkTreatmentContract"]

        class MissingCanvasAdapters(DeterministicFixtureAdapters):
            def analyze_source(self, source_image, replacement_strategy):
                analysis = super().analyze_source(source_image, replacement_strategy)
                del analysis[canvas_field]
                return analysis

        missing_adapters = MissingCanvasAdapters(FIXTURE)
        missing = self.run_stage(1, missing_adapters)
        self.assertEqual("failed", missing.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], missing.error_code)
        self.assertEqual([], missing_adapters.submission_calls)
        self.assertFalse((missing.output_dir / "generation-package.json").exists())

        class IllegalStickerRemovalAdapters(DeterministicFixtureAdapters):
            def analyze_source(self, source_image, replacement_strategy):
                analysis = super().analyze_source(source_image, replacement_strategy)
                analysis[mark_contract["field"]] = {
                    "assessed": True,
                    "treatments": [
                        {
                            "markId": "ordinary-sticker",
                            "type": "sticker",
                            "region": "画面角落的装饰贴纸",
                            "action": "remove",
                            "basis": "source_pollution",
                            "evidence": "未获得用户删除授权",
                        }
                    ],
                    "evidence": "测试非法删除策略",
                }
                return analysis

        illegal = run_production(
            {**self.request, "productionItemId": "illegal-sticker-removal"},
            self.output_root,
            IllegalStickerRemovalAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
            stage=1,
        )
        self.assertEqual("failed", illegal.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], illegal.error_code)
        self.assertFalse((illegal.output_dir / "generation-package.json").exists())

    def test_four_public_stages_resume_one_item_and_stop_at_real_boundaries(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)

        replacement = self.run_stage(1, adapters)
        self.assertEqual("completed", replacement.outcome)
        self.assertEqual("replacement", replacement.major_stage)
        self.assertEqual("replacement-package.json", replacement.primary_artifact.name)
        self.assertEqual([], adapters.submission_calls)
        self.assertEqual([], adapters.poll_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((replacement.output_dir / "generation-task.json").exists())
        self.assertEqual(
            [],
            validate_production_manifest_lineage(
                replacement.output_dir,
                load_json(replacement.output_dir / "production-manifest.json"),
            ),
        )

        template_image = self.run_stage("image", adapters)
        self.assertEqual("completed", template_image.outcome)
        self.assertEqual("template_image", template_image.major_stage)
        self.assertEqual("approved-template-image.png", template_image.primary_artifact.name)
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(1, len(adapters.poll_calls))
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((template_image.output_dir / "template-analysis.json").exists())
        self.assertEqual(
            [],
            validate_production_manifest_lineage(
                template_image.output_dir,
                load_json(template_image.output_dir / "production-manifest.json"),
            ),
        )

        template_data = self.run_stage(3, adapters)
        self.assertEqual("completed", template_data.outcome)
        self.assertEqual("template_data", template_data.major_stage)
        self.assertEqual("template-data-package.json", template_data.primary_artifact.name)
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(1, len(adapters.poll_calls))
        self.assertEqual([], adapters.upload_calls)
        data_package = load_json(template_data.primary_artifact)
        self.assertEqual("awaiting_oss_finalization", data_package["status"])
        self.assertTrue(load_json(template_data.output_dir / "validation-report.json")["pass"])
        qualification_contract = RULES["criticalOutcomeContract"]
        qualification = load_json(
            template_data.output_dir / qualification_contract["artifactName"]
        )
        qualification_fields = qualification_contract["fields"]
        result_fields = qualification_contract["requirementResultFields"]
        self.assertTrue(qualification[qualification_fields["pass"]])
        self.assertEqual(
            set(qualification_contract["requirementIds"].values()),
            {
                item[result_fields["identity"]]
                for item in qualification[qualification_fields["requirements"]]
            },
        )
        self.assertFalse((template_data.output_dir / "gallery-template.json").exists())
        self.assertEqual(
            [],
            validate_production_manifest_lineage(
                template_data.output_dir,
                load_json(template_data.output_dir / "production-manifest.json"),
            ),
        )

        final = self.run_stage("final", adapters)
        self.assertEqual("completed", final.outcome)
        self.assertEqual("final", final.major_stage)
        self.assertEqual("gallery-template.json", final.primary_artifact.name)
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(1, len(adapters.poll_calls))
        self.assertEqual(1, len(adapters.upload_calls))
        manifest = load_json(final.output_dir / "production-manifest.json")
        self.assertEqual([], validate_production_manifest_lineage(final.output_dir, manifest))

        repeated_stage_two = self.run_stage(2, adapters)
        self.assertEqual("completed", repeated_stage_two.outcome)
        self.assertEqual("template_image", repeated_stage_two.major_stage)
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(1, len(adapters.upload_calls))

    def test_finalization_replays_the_complete_critical_outcome_qualification(
        self,
    ) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        template_data = self.run_stage(3, adapters)
        self.assertEqual("completed", template_data.outcome)
        qualification_name = RULES["criticalOutcomeContract"]["artifactName"]
        (template_data.output_dir / qualification_name).unlink()

        final = self.run_stage(4, adapters)

        self.assertEqual("blocked", final.outcome)
        self.assertEqual([], adapters.upload_calls)
        self.assertFalse((final.output_dir / "gallery-template.json").exists())

    def test_second_stage_uses_the_fal_queue_api_before_approval(self) -> None:
        class Completed:
            pass

        class Handle:
            request_id = "fal-stage-two-request"

        class Client:
            def __init__(self) -> None:
                self.submit_calls: list[tuple[str, dict]] = []
                self.status_calls: list[tuple[str, str]] = []
                self.result_calls: list[tuple[str, str]] = []

            def submit(self, model: str, *, arguments: dict) -> Handle:
                self.submit_calls.append((model, arguments))
                return Handle()

            def status(self, model: str, request_id: str) -> Completed:
                self.status_calls.append((model, request_id))
                return Completed()

            def result(self, model: str, request_id: str) -> dict:
                self.result_calls.append((model, request_id))
                return {"images": [{"url": "https://fal.example/stage-two.png"}]}

        client = Client()
        image_bytes = DeterministicFixtureAdapters._fixture_image_result(
            FIXTURE / "approved-template-image.ppm"
        )["imageBytes"]
        adapters = FalQueueWorkflowAdapters(
            DeterministicFixtureAdapters(FIXTURE),
            client=client,
            download_bytes=lambda _url: image_bytes,
            sleep=lambda _seconds: None,
        )

        first = self.run_stage(1, adapters)
        self.assertEqual("completed", first.outcome)
        self.assertEqual([], client.submit_calls)

        second = self.run_stage(2, adapters)
        self.assertEqual("completed", second.outcome)
        self.assertEqual("template_image", second.major_stage)
        self.assertEqual(1, len(client.submit_calls))
        self.assertEqual(
            [(RULES["generationExecutionContract"]["fal"]["model"], Handle.request_id)],
            client.status_calls,
        )
        self.assertEqual(client.status_calls, client.result_calls)
        wal = load_json(second.output_dir / "generation-wal.json")
        self.assertEqual(
            RULES["generationExecutionContract"]["providerRoles"]["fal"],
            wal["provider"],
        )
        self.assertTrue(second.primary_artifact.is_file())

    def test_template_data_rerun_preserves_key_across_production_items(self) -> None:
        template_key = self.request["templateKey"]
        observed_final_keys = []

        for production_item_id in ("template-data-run-one", "template-data-run-two"):
            request = {
                **self.request,
                "productionItemId": production_item_id,
                "templateKey": template_key,
            }
            adapters = DeterministicFixtureAdapters(FIXTURE)
            template_data = run_production(
                request,
                self.output_root,
                adapters,
                clock=lambda: FIXED_TIME,
                stage=3,
            )

            self.assertEqual("completed", template_data.outcome)
            draft = load_json(template_data.output_dir / "gallery-template.draft.json")
            self.assertEqual(template_key, draft["key"])

            final = run_production(
                request,
                self.output_root,
                adapters,
                clock=lambda: FIXED_TIME,
                stage=4,
            )
            self.assertEqual("completed", final.outcome)
            observed_final_keys.append(load_json(final.gallery_template)["key"])

        self.assertEqual([template_key, template_key], observed_final_keys)

    def test_invalid_stage_is_rejected_before_output_or_external_calls(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = self.run_stage("five", adapters)

        self.assertEqual("needs_input", result.outcome)
        self.assertEqual(RULES["errorCodes"]["invalidProductionRequest"], result.error_code)
        self.assertEqual([], adapters.submission_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertEqual([], list(self.output_root.iterdir()))

    def test_first_stage_blocks_when_a_bound_subject_component_is_missing_from_closure(self) -> None:
        class IncompleteBoundSubject(DeterministicFixtureAdapters):
            def analyze_source(self, source_image, replacement_strategy):
                analysis = super().analyze_source(source_image, replacement_strategy)
                context = RULES["sourceAuthoringContextContract"]
                fields = context["subjectBindingFields"]
                group_fields = context["subjectBindingGroupFields"]
                analysis[context["subjectBindingField"]][fields["groups"]][0][
                    group_fields["requiredComponents"]
                ].append("cushion")
                return analysis

        adapters = IncompleteBoundSubject(FIXTURE)

        result = self.run_stage(1, adapters)

        self.assertEqual("blocked", result.outcome)
        self.assertIn("主体绑定组", result.message)
        self.assertEqual([], adapters.submission_calls)
        self.assertFalse((result.output_dir / "generation-package.json").exists())

    def test_first_stage_freezes_one_exact_prompt_and_gate_proof(self) -> None:
        result = self.run_stage(1, DeterministicFixtureAdapters(FIXTURE))

        self.assertEqual("completed", result.outcome)
        package = load_json(result.output_dir / "generation-package.json")
        gate_contract = RULES["sourceAuthoringContextContract"][
            "firstStageGateContract"
        ]
        gate = package[gate_contract["field"]]
        fields = gate_contract["fields"]
        self.assertTrue(gate[fields["dependencyClosure"]])
        self.assertTrue(gate[fields["subjectBindings"]])
        self.assertTrue(gate[fields["visualContract"]])
        self.assertTrue(gate[fields["sourceCanvas"]])
        self.assertTrue(gate[fields["sourceMarks"]])
        self.assertTrue(gate[fields["prompt"]])
        self.assertEqual(
            hashlib.sha256(package["prompt"].encode("utf-8")).hexdigest(),
            gate[fields["promptSha256"]],
        )
        requirement_fields = RULES["criticalOutcomeContract"][
            "requirementResultFields"
        ]
        requirements = gate[fields["requirementResults"]]
        self.assertEqual(
            {
                RULES["criticalOutcomeContract"]["requirementIds"][role]
                for role in (
                    "replacementDependencyClosure",
                    "sourceStyleFidelity",
                    "identityFeatureBinding",
                    "multiSubjectClosure",
                    "sourceCanvasNormalization",
                    "sourceMarkPolicy",
                    "generationPromptFrozen",
                )
            },
            {
                item[requirement_fields["identity"]]
                for item in requirements
            },
        )
        self.assertTrue(
            all(item[requirement_fields["pass"]] is True for item in requirements)
        )
        self.assertIn("参考图是媒介、画风、构图和空间关系的唯一视觉事实源", package["prompt"])
        self.assertIn("必须同步替换组件", package["prompt"])
        self.assertLess(
            package["prompt"].index("必须同步替换组件"),
            package["prompt"].index("媒介必须保持"),
        )
        self.assertTrue(package["prompt"].splitlines()[-1].startswith("输出一张图"))
        task_result = self.run_stage(2, DeterministicFixtureAdapters(FIXTURE))
        task = load_json(task_result.output_dir / "generation-task.json")
        self.assertEqual(package["prompt"], task["requestIntent"]["prompt"])

    def test_identity_replacement_cannot_leave_an_independent_second_subject_unchanged(
        self,
    ) -> None:
        fixture = ROOT / "fixtures" / "shadow-release" / "ordinary-person"
        request = load_json(fixture / "request.json")
        request["sourceImage"] = str(fixture / request["sourceImage"])
        request["productionItemId"] = "all-subjects-replaced-together"

        class MissingSecondSubject(DeterministicFixtureAdapters):
            def analyze_source(self, source_image, replacement_strategy):
                analysis = super().analyze_source(source_image, replacement_strategy)
                multi = RULES["multiInstanceContract"]
                graph = analysis[multi["sourceFields"]["componentGraph"]]
                component_fields = multi["componentFields"]
                graph[multi["graphFields"]["components"]].append(
                    {
                        component_fields["identity"]: "second-subject",
                        component_fields["role"]: multi["componentRoles"]["subject"],
                        component_fields["identityUnit"]: "source-person-b",
                        component_fields["visualInstance"]: True,
                        component_fields["uploadAsset"]: None,
                        component_fields["control"]: None,
                        component_fields["container"]: None,
                        component_fields["explanation"]: "同一画面中的第二位独立主体",
                    }
                )
                context = RULES["sourceAuthoringContextContract"]
                continuity = analysis[context["subjectContinuityField"]]
                continuity[context["subjectContinuityFields"]["subjectCount"]] = 2
                binding = analysis[context["subjectBindingField"]]
                binding_fields = context["subjectBindingFields"]
                group_fields = context["subjectBindingGroupFields"]
                binding[binding_fields["groups"]].append(
                    {
                        group_fields["identity"]: "second-subject-group",
                        group_fields["relationship"]: context[
                            "subjectBindingRelationships"
                        ]["independent"],
                        group_fields["identityUnits"]: ["source-person-b"],
                        group_fields["requiredComponents"]: ["second-subject"],
                        group_fields["evidence"]: "第二位主体可独立辨识",
                    }
                )
                return analysis

        adapters = MissingSecondSubject(fixture)
        result = run_production(
            request,
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=1,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertIn("多主体身份换图", result.message)
        self.assertEqual([], adapters.submission_calls)
        self.assertFalse((result.output_dir / "generation-package.json").exists())

    def test_stage_three_failure_cannot_reopen_stage_two_generation(self) -> None:
        first_adapters = DeterministicFixtureAdapters(FIXTURE)
        approved = self.run_stage(2, first_adapters)
        self.assertEqual("completed", approved.outcome)
        self.assertEqual(1, len(first_adapters.submission_calls))

        class BrokenAuthoring(DeterministicFixtureAdapters):
            def analyze_approved(self, approved_image):
                raise RuntimeError("P3 authoring unavailable")

        later_adapters = BrokenAuthoring(FIXTURE)
        failed = self.run_stage(3, later_adapters)

        self.assertEqual("failed", failed.outcome)
        self.assertEqual([], later_adapters.submission_calls)
        self.assertEqual([], later_adapters.poll_calls)


if __name__ == "__main__":
    unittest.main()
