from __future__ import annotations

import json
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

    def test_invalid_stage_is_rejected_before_output_or_external_calls(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = self.run_stage("five", adapters)

        self.assertEqual("needs_input", result.outcome)
        self.assertEqual(RULES["errorCodes"]["invalidProductionRequest"], result.error_code)
        self.assertEqual([], adapters.submission_calls)
        self.assertEqual([], adapters.upload_calls)
        self.assertEqual([], list(self.output_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
