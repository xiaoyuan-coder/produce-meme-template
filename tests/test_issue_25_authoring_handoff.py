from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
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


class CapturingHandoffAdapters(DeterministicFixtureAdapters):
    def __init__(self) -> None:
        super().__init__(FIXTURE)
        self.handoffs: list[dict] = []

    def analyze_approved_with_handoff(
        self, approved_image: Path, authoring_handoff: dict
    ) -> dict:
        self.handoffs.append(copy.deepcopy(authoring_handoff))
        return self.analyze_approved(approved_image)


class Issue25AuthoringHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stage_one_intent_and_stage_two_delta_are_injected_into_stage_three(self) -> None:
        adapters = CapturingHandoffAdapters()
        result = run_production(
            {**self.request, "productionItemId": "authoring-handoff"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=3,
        )

        self.assertEqual("completed", result.outcome)
        intent_path = result.output_dir / "authoring-intent.json"
        handoff_path = result.output_dir / "authoring-handoff.json"
        self.assertTrue(intent_path.is_file())
        self.assertTrue(handoff_path.is_file())
        handoff = load_json(handoff_path)
        self.assertEqual([handoff], adapters.handoffs)
        self.assertEqual(
            load_json(result.output_dir / "replacement-plan.json")["mechanism"],
            handoff["sourceIntent"]["mechanism"],
        )
        self.assertTrue(
            handoff["sourceIntent"]["culturalReferenceDiscovery"]["assessed"]
        )
        self.assertIn("contrastMechanism", handoff["sourceIntent"]["subjectContinuity"])
        self.assertEqual(
            {
                "identityUnitIds": ["source-animal"],
                "subjectComponentIds": ["animal-main"],
                "repeatedIdentityRelationIds": [],
                "subjectCount": 1,
                "bindingMode": "single_subject_control",
            },
            handoff["sourceIntent"]["subjectEditIntent"],
        )
        generation = load_json(result.output_dir / "generation-package.json")
        self.assertIn("culturalReference", generation["sections"])
        self.assertIn("subjectContinuity", generation["sections"])
        self.assertEqual(
            load_json(result.output_dir / "visual-review.json")["decision"],
            handoff["approvedDelta"]["decision"],
        )
        self.assertEqual(
            load_json(result.output_dir / "template-analysis.json")[
                "visualFactSourceSha256"
            ],
            handoff["bindings"]["approvedImageSha256"],
        )
        manifest = load_json(result.output_dir / "production-manifest.json")
        self.assertIn("authoring-intent.json", manifest["artifacts"])
        self.assertIn("authoring-handoff.json", manifest["artifacts"])
        self.assertIn(
            "authoring-handoff.json",
            manifest["artifacts"]["template-analysis.json"]["dependsOn"],
        )

    def test_handoff_is_immutable_across_the_stage_three_adapter_boundary(self) -> None:
        class MutatingHandoff(DeterministicFixtureAdapters):
            def analyze_approved_with_handoff(
                self, approved_image: Path, authoring_handoff: dict
            ) -> dict:
                authoring_handoff["sourceIntent"]["mechanism"]["payoff"] = "changed"
                return self.analyze_approved(approved_image)

        result = run_production(
            {**self.request, "productionItemId": "mutated-authoring-handoff"},
            self.output_root,
            MutatingHandoff(FIXTURE),
            clock=lambda: FIXED_TIME,
            stage=3,
        )

        self.assertEqual("failed", result.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "template-analysis.json").exists())

    def test_stage_one_blocks_when_ip_discovery_or_subject_continuity_is_missing(self) -> None:
        class MissingP1Context(DeterministicFixtureAdapters):
            def analyze_source(self, source_image: Path, replacement_strategy):
                analysis = super().analyze_source(
                    source_image, replacement_strategy
                )
                analysis.pop("culturalReferenceDiscovery")
                analysis.pop("subjectContinuity")
                return analysis

        adapters = MissingP1Context(FIXTURE)
        result = run_production(
            {**self.request, "productionItemId": "missing-p1-context"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=1,
        )

        self.assertEqual("failed", result.outcome)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.submission_calls)
        self.assertFalse((result.output_dir / "replacement-plan.json").exists())

    def test_prompt_template_rejects_hidden_visual_instruction_leak(self) -> None:
        class HiddenPromptLeak(CapturingHandoffAdapters):
            def analyze_approved_with_handoff(
                self, approved_image: Path, authoring_handoff: dict
            ) -> dict:
                analysis = super().analyze_approved_with_handoff(
                    approved_image, authoring_handoff
                )
                analysis["promptTemplate"] += (
                    " 保留宠物照片剪贴与扁平应援图形的混合媒介。"
                )
                return analysis

        result = run_production(
            {**self.request, "productionItemId": "hidden-prompt-leak"},
            self.output_root,
            HiddenPromptLeak(),
            clock=lambda: FIXED_TIME,
            stage=3,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)
        self.assertFalse((result.output_dir / "editable-template-spec.json").exists())

    def test_free_text_alone_cannot_justify_omitting_a_replaceable_subject(self) -> None:
        class OldSubjectOmission(CapturingHandoffAdapters):
            def analyze_approved_with_handoff(
                self, approved_image: Path, authoring_handoff: dict
            ) -> dict:
                analysis = super().analyze_approved_with_handoff(
                    approved_image, authoring_handoff
                )
                analysis["slotCandidates"] = [
                    slot
                    for slot in analysis["slotCandidates"]
                    if slot["semanticRole"] != "subject"
                ]
                analysis["promptTemplate"] = analysis["promptTemplate"].replace(
                    '{{ pet_subject | "柯基犬" }}', "一只小动物"
                ).replace("一只一只", "一只")
                analysis["subjectSlotOmissionEvidence"] = {
                    "reviewed": True,
                    "valueGates": {
                        "userMotivation": True,
                        "visuallyVisible": True,
                        "modelControllable": True,
                        "mechanismPreserved": False,
                    },
                    "reason": "当前 v2 不支持主体传图替换",
                }
                analysis["assetUnitAnalysis"]["uploadUnitCount"] = 0
                analysis["assetUnitAnalysis"]["controlUnitCount"] = 2
                analysis["renderingCoherenceDecision"]["subjectTransfers"] = []
                return analysis

        result = run_production(
            {**self.request, "productionItemId": "old-subject-omission"},
            self.output_root,
            OldSubjectOmission(),
            clock=lambda: FIXED_TIME,
            stage=3,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(RULES["errorCodes"]["contractFailure"], result.error_code)

    def test_default_batch_runs_independent_items_concurrently(self) -> None:
        class ConcurrentStageOne(DeterministicFixtureAdapters):
            def __init__(self) -> None:
                super().__init__(FIXTURE)
                self.lock = threading.Lock()
                self.active = 0
                self.maximum_active = 0

            def analyze_source(self, source_image: Path, replacement_strategy):
                with self.lock:
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                try:
                    time.sleep(0.08)
                    return super().analyze_source(source_image, replacement_strategy)
                finally:
                    with self.lock:
                        self.active -= 1

        adapters = ConcurrentStageOne()
        items = [
            {
                **self.request,
                "productionItemId": f"parallel-stage-one-{index}",
                "templateKey": f"parallel-template-{index}",
            }
            for index in range(4)
        ]
        result = run_production(
            {"batchId": "parallel-four-lanes", "items": items},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=1,
        )

        self.assertEqual([item["productionItemId"] for item in items], [
            item.production_item_id for item in result.items
        ])
        self.assertGreaterEqual(adapters.maximum_active, 2)
        self.assertTrue(all(item.outcome == "completed" for item in result.items))

    def test_five_item_batch_finishes_all_p1_checks_before_five_concurrent_submits(
        self,
    ) -> None:
        class FiveItemGenerationBarrier(DeterministicFixtureAdapters):
            def __init__(self) -> None:
                super().__init__(FIXTURE)
                self.lock = threading.Lock()
                self.analysis_count = 0
                self.submit_active = 0
                self.maximum_submit_active = 0
                self.submit_before_all_analysis = False
                self.submit_barrier = threading.Barrier(5)

            def analyze_source(self, source_image: Path, replacement_strategy):
                analysis = super().analyze_source(
                    source_image, replacement_strategy
                )
                with self.lock:
                    self.analysis_count += 1
                return analysis

            def submit_generation(
                self,
                source_image: Path,
                generation_package: dict,
                generation_task: dict,
            ) -> dict:
                with self.lock:
                    if self.analysis_count != 5:
                        self.submit_before_all_analysis = True
                    self.submit_active += 1
                    self.maximum_submit_active = max(
                        self.maximum_submit_active, self.submit_active
                    )
                try:
                    try:
                        self.submit_barrier.wait(timeout=0.5)
                    except threading.BrokenBarrierError:
                        pass
                    return super().submit_generation(
                        source_image, generation_package, generation_task
                    )
                finally:
                    with self.lock:
                        self.submit_active -= 1

        adapters = FiveItemGenerationBarrier()
        items = [
            {
                **self.request,
                "productionItemId": f"one-shot-generation-{index}",
                "templateKey": f"one-shot-template-{index}",
            }
            for index in range(5)
        ]

        result = run_production(
            {"batchId": "five-item-one-shot-generation", "items": items},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=2,
        )

        self.assertTrue(all(item.outcome == "completed" for item in result.items))
        self.assertFalse(adapters.submit_before_all_analysis)
        self.assertEqual(5, adapters.maximum_submit_active)
        self.assertEqual(5, len(adapters.submission_calls))

        resumed = run_production(
            {"batchId": "five-item-one-shot-generation", "items": items},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
            stage=3,
        )

        self.assertTrue(all(item.outcome == "completed" for item in resumed.items))
        self.assertTrue(all(item.resumed for item in resumed.items))
        self.assertEqual(5, len(adapters.submission_calls))


if __name__ == "__main__":
    unittest.main()
