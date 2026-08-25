from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts.produce_meme_template import (
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
    run_template_test,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
RULES = json.loads(
    (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
)
CONTRACT = RULES["templateTestContract"]
REQUEST_FIELDS = CONTRACT["requestFields"]
CASE_FIELDS = CONTRACT["caseFields"]
REPORT_FIELDS = CONTRACT["reportFields"]
CASE_REPORT_FIELDS = CONTRACT["caseReportFields"]
MODES = CONTRACT["modes"]
FIXED_TIME = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def formal_template() -> dict:
    return {
        "key": "t1-cosy-pet",
        "status": "DRAFT",
        "title": "软垫上的安静时刻",
        "description": "一只小动物蜷卧在软垫上的温柔室内画面。",
        "imageSize": "1024x1024",
        "imageN": 1,
        "kind": "PROMPT",
        "promptTemplate": (
            "一只{{ pet_subject | \"柯基犬\" }}蜷卧在"
            "{{ cushion_look | \"暖黄色软垫\" }}上，"
            "{{ room_mood | \"午后窗光\" }}从侧面照入。"
        ),
        "inputSchema": {
            "version": 2,
            "slots": [
              {
                "id": "pet_subject",
                "label": "主体",
                "required": False,
                "text": {
                    "presentation": "suggestions",
                    "allowCustom": True,
                    "defaultValue": "柯基犬",
                    "placeholder": "描述画面主体",
                    "suggestions": ["垂耳兔", "水豚", "小羊"],
                },
              },
              {
                "id": "cushion_look",
                "label": "软垫",
                "required": False,
                "text": {
                    "presentation": "suggestions",
                    "allowCustom": True,
                    "defaultValue": "暖黄色软垫",
                    "placeholder": "描述软垫颜色或材质",
                    "suggestions": ["深蓝色绒垫", "格纹坐垫", "奶油白绒垫"],
                },
              },
              {
                "id": "room_mood",
                "label": "环境光",
                "required": False,
                "text": {
                    "presentation": "suggestions",
                    "allowCustom": True,
                    "defaultValue": "午后窗光",
                    "placeholder": "描述室内环境光",
                    "suggestions": ["清晨冷光", "夜晚暖灯", "雨天柔光"],
                },
              },
            ],
        },
        "preprocessSteps": [],
        "runtimeSemantics": {
            "version": 1,
            "targetInstances": [
                {"id": "pet", "kind": "content_element", "role": "蜷卧的小动物", "region": "画面中央"},
                {"id": "cushion", "kind": "content_element", "role": "承托主体的软垫", "region": "画面下半部"},
                {"id": "room_light", "kind": "content_element", "role": "室内环境光", "region": "画面背景"},
            ],
            "inputBindings": {
                "pet_subject": {"operation": "replace_content", "targetIds": ["pet"], "distributionPolicy": "replace_as_unit"},
                "cushion_look": {"operation": "replace_content", "targetIds": ["cushion"], "distributionPolicy": "replace_as_unit"},
                "room_mood": {"operation": "replace_content", "targetIds": ["room_light"], "distributionPolicy": "replace_as_unit"},
            },
            "visualContract": {
                "medium": "自然光室内摄影",
                "styleTraits": ["短毛和织物细节清楚"],
                "composition": ["方形近景构图"],
                "relations": ["主体蜷卧在软垫上"],
                "colorAndLight": ["柔和冷暖对比"],
            },
        },
        "metadata": {"tags": ["宠物", "室内"]},
        "cover": "https://fixtures.memebuy.test/gallery/templates/t1-cosy-pet.png",
        "referenceImage": "https://fixtures.memebuy.test/gallery/templates/t1-cosy-pet.png",
    }


def t1_request(template_path: Path) -> dict:
    return {
        REQUEST_FIELDS["templateJsonPath"]: str(template_path),
        REQUEST_FIELDS["templateRevision"]: 7,
        REQUEST_FIELDS["invocationIdentity"]: "t1-public-seam",
        REQUEST_FIELDS["cases"]: [
            {
                CASE_FIELDS["caseIdentity"]: "slot-change",
                CASE_FIELDS["mode"]: MODES["slotEdit"],
                CASE_FIELDS["slotValues"]: {
                    "pet_subject": "垂耳兔",
                    "cushion_look": "深蓝色绒垫",
                    "room_mood": "清晨冷光",
                },
            },
            {
                CASE_FIELDS["caseIdentity"]: "free-change",
                CASE_FIELDS["mode"]: MODES["freeEdit"],
                CASE_FIELDS["freePrompt"]: (
                    "一只水豚趴在格纹坐垫上，夜晚暖灯从右侧照入，"
                    "保持安静室内近景。"
                ),
            },
        ],
    }


class InterruptOnceAdapters(DeterministicFixtureAdapters):
    def __init__(self, fixture_dir: Path) -> None:
        super().__init__(fixture_dir)
        self.interrupted = False

    def poll_generation(self, *args, **kwargs):
        if not self.interrupted:
            self.interrupted = True
            raise SystemExit("simulated process exit")
        return super().poll_generation(*args, **kwargs)


class PermanentFailureAdapters(DeterministicFixtureAdapters):
    def submit_generation(self, source_image, generation_package, generation_task):
        contract = RULES["generationExecutionContract"]
        fields = contract["submissionFields"]
        return {
            fields["status"]: contract["submissionStatuses"]["failed"],
            fields["provider"]: contract["providerRoles"]["deterministicFixture"],
            fields["model"]: "fixture-image-model",
            fields["providerRequestIdentity"]: None,
            fields["failureClass"]: contract["failureClasses"]["permanent"],
            fields["failureReason"]: "fixture permanent failure",
        }


class SubmissionUnknownAdapters(DeterministicFixtureAdapters):
    def submit_generation(self, source_image, generation_package, generation_task):
        self.submission_calls.append({"taskId": generation_task["taskId"]})
        contract = RULES["generationExecutionContract"]
        fields = contract["submissionFields"]
        return {
            fields["status"]: contract["submissionStatuses"]["failed"],
            fields["provider"]: contract["providerRoles"]["deterministicFixture"],
            fields["model"]: "fixture-image-model",
            fields["providerRequestIdentity"]: None,
            fields["failureClass"]: contract["failureClasses"]["submissionUnknown"],
            fields["failureReason"]: "provider response was lost",
        }


class SecretFailureAdapters(PermanentFailureAdapters):
    def submit_generation(self, source_image, generation_package, generation_task):
        result = super().submit_generation(
            source_image, generation_package, generation_task
        )
        fields = RULES["generationExecutionContract"]["submissionFields"]
        result[fields["failureReason"]] = "Authorization: Bearer sk-live-super-secret"
        return result


class VisibleDeviationAdapters(DeterministicFixtureAdapters):
    def inspect_template_test(self, generated_image, review_request):
        result = super().inspect_template_test(generated_image, review_request)
        fields = CONTRACT["reviewFields"]
        result[fields["pass"]] = False
        result[fields["visibleDeviations"]] = ["主体姿势比模板参考图更直立"]
        result[fields["explanation"]] = "开放内容已生效，但姿势有可见漂移"
        return result


class PermanentPollFailureAdapters(DeterministicFixtureAdapters):
    def poll_generation(self, *args, **kwargs):
        contract = RULES["generationExecutionContract"]
        fields = contract["pollResultFields"]
        return {
            fields["status"]: contract["pollStatuses"]["failed"],
            fields["failureClass"]: contract["failureClasses"]["permanent"],
            fields["failureReason"]: "content rejected",
            fields["extension"]: None,
            fields["imageBytes"]: None,
            fields["outputAssets"]: [],
            fields["providerOutputIdentity"]: None,
        }


class RetryablePollAdapters(DeterministicFixtureAdapters):
    def poll_generation(self, source_image, package, task, submission):
        self.poll_calls.append({"taskId": task["taskId"]})
        contract = RULES["generationExecutionContract"]
        fields = contract["pollResultFields"]
        return {
            fields["status"]: contract["pollStatuses"]["failed"],
            fields["failureClass"]: contract["failureClasses"]["retryable"],
            fields["failureReason"]: "temporary",
            fields["extension"]: None,
            fields["imageBytes"]: None,
            fields["outputAssets"]: [],
            fields["providerOutputIdentity"]: None,
        }


class MutatingSubmitAdapters(DeterministicFixtureAdapters):
    def submit_generation(self, source_image, generation_package, generation_task):
        result = super().submit_generation(
            source_image, generation_package, generation_task
        )
        generation_package["prompt"] = "OLD LOCKED SUBJECT"
        source_image.unlink()
        return result


class InterruptBeforeReviewAdapters(DeterministicFixtureAdapters):
    def inspect_template_test(self, generated_image, review_request):
        raise SystemExit("candidate persisted before review")


class FailedReviewAdapters(DeterministicFixtureAdapters):
    def inspect_template_test(self, generated_image, review_request):
        raise RuntimeError("review service unavailable")


class InvalidReviewAdapters(DeterministicFixtureAdapters):
    def inspect_template_test(self, generated_image, review_request):
        return {}


class ExitDuringSubmitAdapters(DeterministicFixtureAdapters):
    def submit_generation(self, source_image, generation_package, generation_task):
        raise SystemExit("submit outcome unavailable")


class Issue14TemplateJsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.production_dir = self.root / "existing-production"
        self.production_dir.mkdir()
        self.template_path = self.production_dir / "gallery-template.json"
        self.template_path.write_text(
            json.dumps(formal_template(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.production_manifest = self.production_dir / "production-manifest.json"
        self.production_manifest.write_text(
            json.dumps({"state": RULES["resultStates"]["completed"]}),
            encoding="utf-8",
        )
        self.output = self.root / "template-tests"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_t1_runs_both_edit_modes_without_mutating_production(self) -> None:
        template_before = self.template_path.read_bytes()
        manifest_before = self.production_manifest.read_bytes()
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            t1_request(self.template_path),
            self.output,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", result.outcome)
        report = load_json(result.report_path)
        self.assertEqual(
            hashlib.sha256(template_before).hexdigest(),
            report[REPORT_FIELDS["templateJsonSha256"]],
        )
        self.assertEqual(7, report[REPORT_FIELDS["templateRevision"]])
        self.assertEqual(
            formal_template()["referenceImage"],
            report[REPORT_FIELDS["templateImageUrl"]],
        )
        self.assertEqual(2, len(report[REPORT_FIELDS["cases"]]))
        self.assertEqual(2, len(adapters.submission_calls))
        self.assertEqual(2, len(adapters.poll_calls))
        self.assertEqual([], adapters.upload_calls)
        self.assertEqual(template_before, self.template_path.read_bytes())
        self.assertEqual(manifest_before, self.production_manifest.read_bytes())
        self.assertFalse((result.output_dir / "production-manifest.json").exists())

    def test_t1_output_root_cannot_overlap_the_production_workspace(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            t1_request(self.template_path),
            self.production_dir,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["invalidRequest"], result.error_code)
        self.assertEqual([], adapters.submission_calls)
        self.assertEqual(
            {"gallery-template.json", "production-manifest.json"},
            {path.name for path in self.production_dir.iterdir()},
        )

    def test_slot_and_free_edit_normalize_to_the_actual_generation_prompt(self) -> None:
        result = run_template_test(
            t1_request(self.template_path),
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        report = load_json(result.report_path)
        cases = {
            item[CASE_REPORT_FIELDS["caseIdentity"]]: item
            for item in report[REPORT_FIELDS["cases"]]
        }
        slot_prompt = cases["slot-change"][CASE_REPORT_FIELDS["resolvedPrompt"]]
        self.assertIn("垂耳兔", slot_prompt)
        self.assertIn("深蓝色绒垫", slot_prompt)
        self.assertIn("清晨冷光", slot_prompt)
        self.assertNotIn("{{", slot_prompt)
        free_prompt = cases["free-change"][CASE_REPORT_FIELDS["resolvedPrompt"]]
        self.assertTrue(
            free_prompt.startswith(
                t1_request(self.template_path)[REQUEST_FIELDS["cases"]][1][
                    CASE_FIELDS["freePrompt"]
                ]
            )
        )
        for item in cases.values():
            generation = item[CASE_REPORT_FIELDS["generationRequest"]]
            self.assertEqual(
                item[CASE_REPORT_FIELDS["resolvedPrompt"]],
                generation[CONTRACT["generationRequestFields"]["prompt"]],
            )
            self.assertEqual(
                formal_template()["runtimeSemantics"],
                generation[
                    CONTRACT["generationRequestFields"]["runtimeSemantics"]
                ],
            )
            self.assertEqual(
                CONTRACT["defaultImageCount"],
                generation[CONTRACT["generationRequestFields"]["imageCount"]],
            )

    def test_v2_all_image_slots_prepare_slot_case_from_literal_fallbacks(self) -> None:
        template = formal_template()
        for slot in template["inputSchema"]["slots"]:
            slot.pop("text")
            slot["image"] = {
                "promptValue": f"用户上传图中的{slot['label']}",
                "hint": f"上传1张{slot['label']}图片",
                "maxCount": 1,
                "minWidth": 256,
                "minHeight": 256,
                "private": True,
                "sourceOptions": ["upload", "recent_upload", "asset_library"],
            }
        self.template_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = [
            {
                CASE_FIELDS["caseIdentity"]: "image-defaults",
                CASE_FIELDS["mode"]: MODES["slotEdit"],
                CASE_FIELDS["slotValues"]: {},
            }
        ]

        result = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", result.outcome)
        report = load_json(result.report_path)
        resolved = report[REPORT_FIELDS["cases"]][0][
            CASE_REPORT_FIELDS["resolvedPrompt"]
        ]
        self.assertIn("柯基犬", resolved)
        self.assertIn("暖黄色软垫", resolved)
        self.assertIn("午后窗光", resolved)
        self.assertNotIn("{{", resolved)

    def test_optional_author_fields_may_be_omitted_and_distribution_fields_are_accepted(self) -> None:
        template = formal_template()
        for field in ("description", "imageN", "kind", "preprocessSteps", "metadata"):
            template.pop(field)
        template["communityKey"] = "fixture-community"
        template["featureKeys"] = ["fixture-feature"]
        self.template_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result = run_template_test(
            t1_request(self.template_path),
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", result.outcome)

    def test_non_slot_copy_is_stable_in_slot_edit_and_editable_in_whole_prompt_mode(self) -> None:
        default_secondary_copy = "EXPOSITION\nPeinture—Sculpture"
        edited_secondary_copy = "WEEKEND SHOW\nPainting—Sculpture"
        template = load_json(self.template_path)
        template["promptTemplate"] += (
            f"副文字两行写着“{default_secondary_copy}”。"
        )
        self.template_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]][1][CASE_FIELDS["freePrompt"]] = (
            "一只水豚趴在格纹坐垫上，夜晚暖灯从右侧照入，"
            f"副文字改为“{edited_secondary_copy}”。"
        )

        result = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        report = load_json(result.report_path)
        cases = {
            item[CASE_REPORT_FIELDS["caseIdentity"]]: item
            for item in report[REPORT_FIELDS["cases"]]
        }
        self.assertIn(
            default_secondary_copy,
            cases["slot-change"][CASE_REPORT_FIELDS["resolvedPrompt"]],
        )
        self.assertNotIn(
            edited_secondary_copy,
            cases["slot-change"][CASE_REPORT_FIELDS["resolvedPrompt"]],
        )
        self.assertTrue(
            cases["free-change"][CASE_REPORT_FIELDS["resolvedPrompt"]].startswith(
                request[REQUEST_FIELDS["cases"]][1][CASE_FIELDS["freePrompt"]]
            )
        )
        self.assertIn(
            edited_secondary_copy,
            cases["free-change"][CASE_REPORT_FIELDS["resolvedPrompt"]],
        )

    def test_submitted_case_resumes_without_a_second_submit(self) -> None:
        adapters = InterruptOnceAdapters(FIXTURE)
        request = t1_request(self.template_path)
        with self.assertRaises(SystemExit):
            run_template_test(
                request, self.output, adapters, clock=lambda: FIXED_TIME
            )

        resumed = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("completed", resumed.outcome)
        first_task = adapters.submission_calls[0]["taskId"]
        self.assertEqual(
            1,
            sum(
                call["taskId"] == first_task
                for call in adapters.submission_calls
            ),
        )
        self.assertTrue(resumed.resumed)

    def test_invalid_formal_json_stops_before_generation(self) -> None:
        template = formal_template()
        template["cover"] = "https://fixtures.memebuy.test/wrong.png"
        self.template_path.write_text(json.dumps(template), encoding="utf-8")
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            t1_request(self.template_path),
            self.output,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["invalidTemplate"], result.error_code)
        self.assertEqual([], adapters.submission_calls)
        self.assertEqual([], adapters.poll_calls)

    def test_formal_projection_extra_field_stops_before_generation(self) -> None:
        template = formal_template()
        template["imageN"] = 4
        self.template_path.write_text(json.dumps(template), encoding="utf-8")
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            t1_request(self.template_path),
            self.output,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["invalidTemplate"], result.error_code)
        self.assertEqual([], adapters.submission_calls)

    def test_formal_prompt_placeholders_must_exactly_bind_input_schema(self) -> None:
        template = formal_template()
        template["promptTemplate"] = '一只{{ ghost | "柯基犬" }}在室内。'
        self.template_path.write_text(json.dumps(template), encoding="utf-8")
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            t1_request(self.template_path),
            self.output,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["invalidTemplate"], result.error_code)
        self.assertEqual([], adapters.submission_calls)

    def test_legacy_pure_image_slot_is_rejected_by_the_v2_formal_contract(self) -> None:
        template = formal_template()
        template["promptTemplate"] = '把{{ photo | "默认照片" }}放在画面中央。'
        template["inputSchema"] = [
            {"type": "image", "id": "photo", "label": "照片"}
        ]
        self.template_path.write_text(json.dumps(template), encoding="utf-8")
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = [
            {
                CASE_FIELDS["caseIdentity"]: "image-change",
                CASE_FIELDS["mode"]: MODES["slotEdit"],
                CASE_FIELDS["slotValues"]: {"photo": "asset-123"},
            }
        ]
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["invalidTemplate"], result.error_code)
        self.assertEqual([], adapters.submission_calls)

    def test_existing_completed_invocation_is_create_once_and_resumable(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        request = t1_request(self.template_path)
        first = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )
        template_before = self.template_path.read_bytes()
        second = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("completed", first.outcome)
        self.assertEqual("completed", second.outcome)
        self.assertTrue(second.resumed)
        self.assertEqual(2, len(adapters.submission_calls))
        self.assertEqual(2, len(adapters.poll_calls))
        self.assertEqual(template_before, self.template_path.read_bytes())

    def test_completed_report_must_still_cover_every_frozen_case(self) -> None:
        request = t1_request(self.template_path)
        first = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        report = load_json(first.report_path)
        report[REPORT_FIELDS["cases"]] = []
        payload = (
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        first.report_path.write_bytes(payload)
        manifest_path = first.output_dir / CONTRACT["artifactNames"]["manifest"]
        manifest = load_json(manifest_path)
        manifest[CONTRACT["manifestFields"]["reportSha256"]] = hashlib.sha256(
            payload
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )

        resumed = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)

    def test_completed_resume_replays_task_wal_review_and_case_semantics(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        first = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        case_dir = first.output_dir / f"case-{case_id}"
        (case_dir / CONTRACT["artifactNames"]["review"]).unlink()

        resumed = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)

    def test_submitted_wal_request_id_is_bound_to_immutable_submission(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = InterruptOnceAdapters(FIXTURE)
        with self.assertRaises(SystemExit):
            run_template_test(request, self.output, adapters, clock=lambda: FIXED_TIME)
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        wal_path = (
            self.output
            / request[REQUEST_FIELDS["invocationIdentity"]]
            / f"case-{case_id}"
            / CONTRACT["artifactNames"]["wal"]
        )
        wal = load_json(wal_path)
        wal[RULES["generationExecutionContract"]["walFields"]["providerRequestIdentity"]] = (
            "attacker-request-999"
        )
        wal_path.write_text(json.dumps(wal), encoding="utf-8")
        submission_path = wal_path.with_name(CONTRACT["artifactNames"]["submission"])
        submission = load_json(submission_path)
        submission[
            RULES["generationExecutionContract"]["submissionFields"][
                "providerRequestIdentity"
            ]
        ] = "attacker-request-999"
        submission_path.write_text(json.dumps(submission), encoding="utf-8")

        resumed = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)
        self.assertEqual(0, len(adapters.poll_calls))

    def test_running_reference_image_drift_is_an_integrity_failure(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = InterruptOnceAdapters(FIXTURE)
        with self.assertRaises(SystemExit):
            run_template_test(request, self.output, adapters, clock=lambda: FIXED_TIME)
        invocation_dir = self.output / request[REQUEST_FIELDS["invocationIdentity"]]
        reference = next(invocation_dir.glob("template-reference-image.*"))
        reference.with_suffix(".jpg").write_bytes(reference.read_bytes())

        resumed = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)

    def test_running_task_cannot_be_silently_recreated(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = InterruptOnceAdapters(FIXTURE)
        with self.assertRaises(SystemExit):
            run_template_test(request, self.output, adapters, clock=lambda: FIXED_TIME)
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        task_path = (
            self.output
            / request[REQUEST_FIELDS["invocationIdentity"]]
            / f"case-{case_id}"
            / CONTRACT["artifactNames"]["task"]
        )
        task_path.unlink()

        resumed = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)
        self.assertFalse(task_path.exists())

    def test_succeeded_candidate_drift_before_review_is_integrity_failure(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = InterruptBeforeReviewAdapters(FIXTURE)
        with self.assertRaises(SystemExit):
            run_template_test(request, self.output, adapters, clock=lambda: FIXED_TIME)
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        candidate = next(
            (self.output / request[REQUEST_FIELDS["invocationIdentity"]] / f"case-{case_id}").glob(
                "generated-image.*"
            )
        )
        candidate.unlink()

        resumed = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)

    def test_adapter_mutation_is_isolated_from_frozen_images_and_requests(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]

        result = run_template_test(
            request,
            self.output,
            MutatingSubmitAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("failed", result.outcome)
        references = list(result.output_dir.glob("template-reference-image.*"))
        self.assertEqual(1, len(references))
        self.assertTrue(references[0].is_file())

    def test_formal_template_drift_during_review_blocks_before_final_report(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        template_path = self.template_path

        class ConcurrentTemplateMutationAdapters(DeterministicFixtureAdapters):
            def inspect_template_test(self, generated_image, review_request):
                result = super().inspect_template_test(
                    generated_image, review_request
                )
                changed = load_json(template_path)
                changed["title"] = "concurrently changed"
                template_path.write_text(json.dumps(changed), encoding="utf-8")
                return result

        result = run_template_test(
            request,
            self.output,
            ConcurrentTemplateMutationAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], result.error_code)
        manifest = load_json(
            result.output_dir / CONTRACT["artifactNames"]["manifest"]
        )
        self.assertNotEqual(
            CONTRACT["states"]["completed"],
            manifest[CONTRACT["manifestFields"]["state"]],
        )
        self.assertFalse(
            (result.output_dir / CONTRACT["artifactNames"]["report"]).exists()
        )

    def test_untracked_preseeded_reference_image_is_rejected(self) -> None:
        request = t1_request(self.template_path)
        invocation_dir = self.output / request[REQUEST_FIELDS["invocationIdentity"]]
        invocation_dir.mkdir(parents=True)
        image = DeterministicFixtureAdapters._fixture_image_result(
            FIXTURE / "approved-template-image.ppm"
        )["imageBytes"]
        (invocation_dir / "template-reference-image.png").write_bytes(image)
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", result.outcome)
        self.assertEqual([], adapters.submission_calls)

    def test_report_before_manifest_crash_is_forward_recovered(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        first = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual("completed", first.outcome)
        manifest_path = (
            first.output_dir / CONTRACT["artifactNames"]["manifest"]
        )
        manifest = load_json(manifest_path)
        manifest[CONTRACT["manifestFields"]["state"]] = CONTRACT["states"][
            "running"
        ]
        manifest[CONTRACT["manifestFields"]["reportSha256"]] = None
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

        later = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
        resumed = run_template_test(
            request,
            self.output,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: later,
        )

        self.assertEqual("completed", resumed.outcome)
        self.assertTrue(resumed.resumed)

    def test_submission_unknown_pauses_without_recommending_a_new_submission(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = SubmissionUnknownAdapters(FIXTURE)

        first = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )
        second = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("needs_input", first.outcome)
        self.assertEqual("needs_input", second.outcome)
        self.assertEqual(
            CONTRACT["errorCodes"]["generationSubmissionUnknown"],
            first.error_code,
        )
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(0, len(adapters.poll_calls))

    def test_unbound_preseeded_submission_never_becomes_a_pollable_request(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        with self.assertRaises(SystemExit):
            run_template_test(
                request,
                self.output,
                ExitDuringSubmitAdapters(FIXTURE),
                clock=lambda: FIXED_TIME,
            )
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        case_dir = (
            self.output
            / request[REQUEST_FIELDS["invocationIdentity"]]
            / f"case-{case_id}"
        )
        execution = RULES["generationExecutionContract"]
        fields = execution["submissionFields"]
        injected = {
            fields["status"]: execution["submissionStatuses"]["submitted"],
            fields["provider"]: execution["providerRoles"]["deterministicFixture"],
            fields["model"]: "fixture-image-model",
            fields["providerRequestIdentity"]: "attacker-request-001",
            fields["failureClass"]: None,
            fields["failureReason"]: None,
        }
        (case_dir / CONTRACT["artifactNames"]["submission"]).write_text(
            json.dumps(injected), encoding="utf-8"
        )
        recovery = DeterministicFixtureAdapters(FIXTURE)

        resumed = run_template_test(
            request, self.output, recovery, clock=lambda: FIXED_TIME
        )

        self.assertEqual("needs_input", resumed.outcome)
        self.assertEqual([], recovery.submission_calls)
        self.assertEqual([], recovery.poll_calls)

    def test_t1_is_an_explicit_subcommand_of_the_existing_entrypoint(self) -> None:
        request_path = self.root / "t1-request.json"
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["templateJsonPath"]] = str(
            self.template_path.relative_to(self.root)
        )
        request_path.write_text(json.dumps(request), encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "produce.py"),
                "t1",
                "--request",
                str(request_path),
                "--output",
                str(self.output),
                "--deterministic-fixture",
                str(FIXTURE),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        result_fields = CONTRACT["resultFields"]
        self.assertEqual("completed", payload[result_fields["outcome"]])
        self.assertFalse((self.output / "production-manifest.json").exists())

    def test_t1_cli_malformed_request_returns_machine_json_without_traceback(self) -> None:
        request_path = self.root / "bad-request.json"
        request_path.write_text("{bad", encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "produce.py"),
                "t1",
                "--request",
                str(request_path),
                "--output",
                str(self.output),
                "--deterministic-fixture",
                str(FIXTURE),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(
            CONTRACT["errorCodes"]["invalidRequest"],
            payload[CONTRACT["resultFields"]["errorCode"]],
        )

    def test_visible_deviation_is_recorded_without_changing_execution_success(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]

        result = run_template_test(
            request,
            self.output,
            VisibleDeviationAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("completed", result.outcome)
        report = load_json(result.report_path)
        case = report[REPORT_FIELDS["cases"]][0]
        self.assertFalse(case[CASE_REPORT_FIELDS["reviewPass"]])
        self.assertEqual(
            ["主体姿势比模板参考图更直立"],
            case[CASE_REPORT_FIELDS["visibleDeviations"]],
        )

    def test_terminal_generation_failure_writes_an_immutable_failure_report(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        template_before = self.template_path.read_bytes()

        result = run_template_test(
            request,
            self.output,
            PermanentFailureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("failed", result.outcome)
        self.assertIsNotNone(result.report_path)
        report = load_json(result.report_path)
        self.assertEqual("failed", report[REPORT_FIELDS["outcome"]])
        self.assertEqual(
            CONTRACT["errorCodes"]["generationPermanent"],
            report[REPORT_FIELDS["errorCode"]],
        )
        self.assertEqual(
            "failed",
            report[REPORT_FIELDS["cases"]][0][CASE_REPORT_FIELDS["outcome"]],
        )
        self.assertEqual(
            report[REPORT_FIELDS["cases"]][0][CASE_REPORT_FIELDS["resolvedPrompt"]],
            report[REPORT_FIELDS["cases"]][0][CASE_REPORT_FIELDS["generationRequest"]][
                CONTRACT["generationRequestFields"]["prompt"]
            ],
        )
        self.assertEqual(template_before, self.template_path.read_bytes())

    def test_provider_failure_secrets_are_not_persisted(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]

        result = run_template_test(
            request,
            self.output,
            SecretFailureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("failed", result.outcome)
        persisted = "\n".join(
            path.read_text(encoding="utf-8")
            for path in result.output_dir.rglob("*.json")
        )
        self.assertNotIn("sk-live-super-secret", persisted)
        self.assertNotIn("Authorization: Bearer", persisted)

    def test_failed_report_replays_the_frozen_case_input(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        first = run_template_test(
            request,
            self.output,
            PermanentFailureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        report = load_json(first.report_path)
        report[REPORT_FIELDS["cases"]][0][
            CASE_REPORT_FIELDS["resolvedPrompt"]
        ] = "FORGED PROMPT"
        payload = (
            json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        first.report_path.write_bytes(payload)
        manifest_path = first.output_dir / CONTRACT["artifactNames"]["manifest"]
        manifest = load_json(manifest_path)
        manifest[CONTRACT["manifestFields"]["reportSha256"]] = hashlib.sha256(
            payload
        ).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        resumed = run_template_test(
            request,
            self.output,
            PermanentFailureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)

    def test_failed_resume_requires_terminal_task_wal_and_submission(self) -> None:
        for index, artifact_role in enumerate(("task", "wal", "submission"), 1):
            with self.subTest(artifact=artifact_role):
                request = t1_request(self.template_path)
                request[REQUEST_FIELDS["invocationIdentity"]] = f"t1-terminal-{index}"
                request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
                first = run_template_test(
                    request,
                    self.output,
                    PermanentFailureAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                )
                case_id = request[REQUEST_FIELDS["cases"]][0][
                    CASE_FIELDS["caseIdentity"]
                ]
                (
                    first.output_dir
                    / f"case-{case_id}"
                    / CONTRACT["artifactNames"][artifact_role]
                ).unlink()

                resumed = run_template_test(
                    request,
                    self.output,
                    PermanentFailureAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual("blocked", resumed.outcome)
                self.assertEqual(
                    CONTRACT["errorCodes"]["integrityFailure"],
                    resumed.error_code,
                )

    def test_failed_review_resume_still_requires_the_succeeded_candidate(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        first = run_template_test(
            request,
            self.output,
            FailedReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        self.assertEqual("failed", first.outcome)
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        candidate = next(
            (first.output_dir / f"case-{case_id}").glob("generated-image.*")
        )
        candidate.unlink()

        resumed = run_template_test(
            request,
            self.output,
            FailedReviewAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)

    def test_invalid_review_is_not_persisted_and_failure_resume_is_stable(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = InvalidReviewAdapters(FIXTURE)

        first = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )
        second = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("failed", first.outcome)
        self.assertEqual("failed", second.outcome)
        self.assertTrue(second.resumed)
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        self.assertFalse(
            (
                first.output_dir
                / f"case-{case_id}"
                / CONTRACT["artifactNames"]["review"]
            ).exists()
        )

    def test_permanent_poll_failure_is_frozen_in_wal_and_report(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]

        result = run_template_test(
            request,
            self.output,
            PermanentPollFailureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("failed", result.outcome)
        self.assertEqual(
            CONTRACT["errorCodes"]["generationPermanent"], result.error_code
        )
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        wal = load_json(
            result.output_dir
            / f"case-{case_id}"
            / CONTRACT["artifactNames"]["wal"]
        )
        execution = RULES["generationExecutionContract"]
        self.assertEqual(
            execution["walStatuses"]["failed"],
            wal[execution["walFields"]["status"]],
        )
        self.assertEqual(
            execution["failureClasses"]["permanent"],
            wal[execution["walFields"]["failureClass"]],
        )

    def test_retryable_poll_count_cannot_be_rolled_back_outside_manifest(self) -> None:
        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        adapters = RetryablePollAdapters(FIXTURE)
        first = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )
        second = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )
        self.assertEqual("failed", first.outcome)
        self.assertEqual("failed", second.outcome)
        case_id = request[REQUEST_FIELDS["cases"]][0][CASE_FIELDS["caseIdentity"]]
        wal_path = (
            second.output_dir
            / f"case-{case_id}"
            / CONTRACT["artifactNames"]["wal"]
        )
        wal = load_json(wal_path)
        execution = RULES["generationExecutionContract"]
        count_field = execution["walFields"]["pollAttemptCount"]
        self.assertEqual(2, wal[count_field])
        wal[count_field] = 1
        wal_path.write_text(json.dumps(wal), encoding="utf-8")
        polls_before = len(adapters.poll_calls)

        resumed = run_template_test(
            request, self.output, adapters, clock=lambda: FIXED_TIME
        )

        self.assertEqual("blocked", resumed.outcome)
        self.assertEqual(CONTRACT["errorCodes"]["integrityFailure"], resumed.error_code)
        self.assertEqual(polls_before, len(adapters.poll_calls))

    def test_real_fal_adapter_resumes_the_same_request_after_retryable_poll(self) -> None:
        class Handle:
            request_id = "fal-t1-request-001"

        class InterruptedClient:
            def __init__(self) -> None:
                self.submit_calls = 0
                self.arguments: dict | None = None

            def submit(self, _model, *, arguments):
                self.submit_calls += 1
                self.arguments = copy.deepcopy(arguments)
                return Handle()

            def status(self, _model, _request_id):
                raise TimeoutError("temporary status timeout")

        class Completed:
            pass

        class RecoveryClient:
            def __init__(self) -> None:
                self.submit_calls = 0
                self.status_ids: list[str] = []

            def submit(self, _model, *, arguments):
                self.submit_calls += 1
                raise AssertionError("recovery cannot submit again")

            def status(self, _model, request_id):
                self.status_ids.append(request_id)
                return Completed()

            def result(self, _model, _request_id):
                return {"images": [{"url": "https://fal.example/t1.png"}]}

        request = t1_request(self.template_path)
        request[REQUEST_FIELDS["cases"]] = request[REQUEST_FIELDS["cases"]][:1]
        image_bytes = DeterministicFixtureAdapters._fixture_image_result(
            FIXTURE / "approved-template-image.ppm"
        )["imageBytes"]
        first_client = InterruptedClient()
        first = run_template_test(
            request,
            self.output,
            FalQueueWorkflowAdapters(
                DeterministicFixtureAdapters(FIXTURE),
                client=first_client,
                download_bytes=lambda _url: image_bytes,
                sleep=lambda _seconds: None,
            ),
            clock=lambda: FIXED_TIME,
        )
        recovery_client = RecoveryClient()
        second = run_template_test(
            request,
            self.output,
            FalQueueWorkflowAdapters(
                DeterministicFixtureAdapters(FIXTURE),
                client=recovery_client,
                download_bytes=lambda _url: image_bytes,
                sleep=lambda _seconds: None,
            ),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual("failed", first.outcome)
        self.assertEqual(
            CONTRACT["errorCodes"]["generationRetryable"], first.error_code
        )
        self.assertEqual("completed", second.outcome)
        self.assertEqual(1, first_client.submit_calls)
        self.assertIsNotNone(first_client.arguments)
        actual_prompt = first_client.arguments["prompt"]
        self.assertIn("垂耳兔", actual_prompt)
        self.assertIn("自然光室内摄影", actual_prompt)
        self.assertIn("画面中央", actual_prompt)
        self.assertIn("主体蜷卧在软垫上", actual_prompt)
        self.assertEqual(0, recovery_client.submit_calls)
        self.assertEqual([Handle.request_id], recovery_client.status_ids)


if __name__ == "__main__":
    unittest.main()
