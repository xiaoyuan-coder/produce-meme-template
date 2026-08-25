from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path

from scripts.produce_meme_template import (
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
    run_production,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
FIXED_TIME = datetime.fromisoformat("2026-08-17T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


RULES = load_json(ROOT / "contracts" / "machine-rules.json")
CONTRACT = RULES["generationExecutionContract"]
TASK_FIELDS = CONTRACT["taskFields"]
INTENT_FIELDS = CONTRACT["requestIntentFields"]
WAL_FIELDS = CONTRACT["walFields"]
SUBMISSION_FIELDS = CONTRACT["submissionFields"]
POLL_FIELDS = CONTRACT["pollResultFields"]
ASSET_FIELDS = CONTRACT["outputAssetFields"]


class QueuedFixtureAdapters(DeterministicFixtureAdapters):
    def __init__(
        self,
        fixture_dir: Path,
        expected_output_dir: Path,
        *,
        poll_results: list[dict] | None = None,
        submit_exception: Exception | None = None,
    ) -> None:
        super().__init__(fixture_dir)
        self.expected_output_dir = expected_output_dir
        self.poll_results = list(poll_results or [])
        self.submit_exception = submit_exception
        self.submission_calls: list[dict] = []
        self.poll_calls: list[dict] = []

    def submit_generation(
        self, source_image: Path, generation_package: dict, generation_task: dict
    ) -> dict:
        revision = generation_task[TASK_FIELDS["revision"]]
        suffix = "" if revision == 1 else f"-r{revision}"
        task_path = self.expected_output_dir / (
            f"{CONTRACT['artifactTypes']['task']}{suffix}.json"
        )
        wal_path = self.expected_output_dir / (
            f"{CONTRACT['artifactTypes']['wal']}{suffix}.json"
        )
        self.assert_pre_submit_state(task_path, wal_path, generation_task)
        self.submission_calls.append(
            {
                "taskId": generation_task[TASK_FIELDS["taskIdentity"]],
                "requestId": generation_package["requestId"],
            }
        )
        if self.submit_exception is not None:
            raise self.submit_exception
        return {
            SUBMISSION_FIELDS["status"]: CONTRACT["submissionStatuses"]["submitted"],
            SUBMISSION_FIELDS["provider"]: CONTRACT["providerRoles"][
                "deterministicFixture"
            ],
            SUBMISSION_FIELDS["model"]: "fixture-image-model",
            SUBMISSION_FIELDS["providerRequestIdentity"]: (
                "provider-" + generation_task[TASK_FIELDS["taskIdentity"]]
            ),
            SUBMISSION_FIELDS["failureClass"]: None,
            SUBMISSION_FIELDS["failureReason"]: None,
        }

    def assert_pre_submit_state(
        self, task_path: Path, wal_path: Path, generation_task: dict
    ) -> None:
        if not task_path.is_file() or not wal_path.is_file():
            raise AssertionError("generation task and prepared WAL must exist before submit")
        if load_json(task_path) != generation_task:
            raise AssertionError("submitted generation task must equal the frozen task")
        wal = load_json(wal_path)
        if wal[WAL_FIELDS["status"]] != CONTRACT["walStatuses"]["prepared"]:
            raise AssertionError("WAL must be prepared before provider submit")
        if wal[WAL_FIELDS["providerRequestIdentity"]] is not None:
            raise AssertionError("prepared WAL cannot invent a provider request ID")

    def poll_generation(
        self,
        source_image: Path,
        generation_package: dict,
        generation_task: dict,
        submission: dict,
    ) -> dict:
        self.poll_calls.append(
            {
                "taskId": generation_task[TASK_FIELDS["taskIdentity"]],
                "providerRequestId": submission[
                    SUBMISSION_FIELDS["providerRequestIdentity"]
                ],
            }
        )
        if self.poll_results:
            return self.poll_results.pop(0)
        generated = super().generate(source_image, generation_package)
        image_sha = hashlib.sha256(generated["imageBytes"]).hexdigest()
        image_count = generation_task[TASK_FIELDS["requestIntent"]][
            INTENT_FIELDS["imageCount"]
        ]
        return {
            POLL_FIELDS["status"]: CONTRACT["pollStatuses"]["succeeded"],
            POLL_FIELDS["failureClass"]: None,
            POLL_FIELDS["failureReason"]: None,
            POLL_FIELDS["extension"]: generated["extension"],
            POLL_FIELDS["imageBytes"]: generated["imageBytes"],
            POLL_FIELDS["providerOutputIdentity"]: f"fixture-output-{generation_task[TASK_FIELDS['requestIntent']][INTENT_FIELDS['primaryOutputIndex']]}",
            POLL_FIELDS["outputAssets"]: [
                {
                    ASSET_FIELDS["providerOutputIdentity"]: f"fixture-output-{index}",
                    ASSET_FIELDS["sha256"]: image_sha,
                }
                for index in range(image_count)
            ],
        }


def failed_poll(failure_role: str, reason: str) -> dict:
    return {
        POLL_FIELDS["status"]: CONTRACT["pollStatuses"]["failed"],
        POLL_FIELDS["failureClass"]: CONTRACT["failureClasses"][failure_role],
        POLL_FIELDS["failureReason"]: reason,
        POLL_FIELDS["extension"]: None,
        POLL_FIELDS["imageBytes"]: None,
        POLL_FIELDS["providerOutputIdentity"]: None,
        POLL_FIELDS["outputAssets"]: [],
    }


class Issue10GenerationWalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def adapters_for(self, item_id: str, **kwargs) -> QueuedFixtureAdapters:
        return QueuedFixtureAdapters(
            FIXTURE,
            self.output_root / item_id,
            **kwargs,
        )

    def run_case(self, item_id: str, adapters: QueuedFixtureAdapters, **request) -> object:
        return run_production(
            {**self.request, "productionItemId": item_id, **request},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

    def rewrite_tracked_json(self, output_dir: Path, name: str, value: dict) -> None:
        path = output_dir / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path = output_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        manifest["artifacts"][name]["sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        manifest["artifacts"][name]["bytes"] = path.stat().st_size
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_default_request_writes_frozen_task_and_wal_before_submit(self) -> None:
        item_id = "queued-generation-default"
        adapters = self.adapters_for(item_id)

        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        self.assertEqual(1, len(adapters.submission_calls))
        self.assertEqual(1, len(adapters.poll_calls))
        task = load_json(result.output_dir / "generation-task.json")
        wal = load_json(result.output_dir / "generation-wal.json")
        pin_sha = hashlib.sha256(
            (result.output_dir / "production-pin.json").read_bytes()
        ).hexdigest()
        package_sha = hashlib.sha256(
            (result.output_dir / "generation-package.json").read_bytes()
        ).hexdigest()
        self.assertEqual(CONTRACT["artifactTypes"]["task"], task[TASK_FIELDS["artifactType"]])
        self.assertEqual(CONTRACT["defaultImageCount"], task[TASK_FIELDS["requestIntent"]][INTENT_FIELDS["imageCount"]])
        self.assertEqual(pin_sha, task[TASK_FIELDS["productionPinSha256"]])
        self.assertEqual(package_sha, task[TASK_FIELDS["generationPackageSha256"]])
        self.assertEqual(CONTRACT["walStatuses"]["succeeded"], wal[WAL_FIELDS["status"]])
        self.assertTrue(wal[WAL_FIELDS["providerRequestIdentity"]])
        self.assertEqual(
            hashlib.sha256(
                (result.output_dir / "evidence" / "generated-candidate-image.png").read_bytes()
            ).hexdigest(),
            wal[WAL_FIELDS["outputSha256"]],
        )

    def test_explicit_image_count_is_frozen_in_task_and_provider_output_evidence(self) -> None:
        item_id = "queued-generation-explicit-count"
        adapters = self.adapters_for(item_id)
        option_field = CONTRACT["requestOptionsField"]
        option_fields = CONTRACT["requestOptionFields"]

        result = self.run_case(
            item_id,
            adapters,
            **{
                option_field: {
                    option_fields["imageCount"]: 2,
                    option_fields["primaryOutputIndex"]: 1,
                }
            },
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        task = load_json(result.output_dir / "generation-task.json")
        intent = task[TASK_FIELDS["requestIntent"]]
        self.assertEqual(2, intent[INTENT_FIELDS["imageCount"]])
        self.assertEqual(1, intent[INTENT_FIELDS["primaryOutputIndex"]])
        wal = load_json(result.output_dir / "generation-wal.json")
        self.assertEqual(2, len(wal["outputAssets"]))

    def test_generation_options_are_validated_and_part_of_production_identity(self) -> None:
        option_field = CONTRACT["requestOptionsField"]
        option_fields = CONTRACT["requestOptionFields"]
        invalid_id = "queued-generation-invalid-options"
        invalid_adapters = self.adapters_for(invalid_id)

        invalid = self.run_case(
            invalid_id,
            invalid_adapters,
            **{
                option_field: {
                    option_fields["imageCount"]: CONTRACT["maximumImageCount"] + 1,
                    option_fields["primaryOutputIndex"]: 0,
                }
            },
        )

        self.assertEqual(RULES["resultStates"]["needs_input"], invalid.state)
        self.assertEqual([], invalid_adapters.submission_calls)
        identity_id = "queued-generation-options-identity"
        first = self.run_case(
            identity_id,
            self.adapters_for(identity_id),
            **{
                option_field: {
                    option_fields["imageCount"]: 2,
                    option_fields["primaryOutputIndex"]: 1,
                }
            },
        )
        changed = self.run_case(identity_id, self.adapters_for(identity_id))
        self.assertEqual(RULES["resultStates"]["completed"], first.state)
        self.assertEqual(RULES["resultStates"]["blocked"], changed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            changed.error_code,
        )

    def test_multiple_provider_outputs_require_unique_provider_identities(self) -> None:
        item_id = "queued-generation-duplicate-output-ids"
        generated = DeterministicFixtureAdapters._fixture_image_result(
            FIXTURE / "approved-template-image.ppm"
        )
        payload = generated["imageBytes"]
        payload_sha = hashlib.sha256(payload).hexdigest()
        duplicate_id = "duplicate-provider-output"
        adapters = self.adapters_for(
            item_id,
            poll_results=[
                {
                    POLL_FIELDS["status"]: CONTRACT["pollStatuses"]["succeeded"],
                    POLL_FIELDS["failureClass"]: None,
                    POLL_FIELDS["failureReason"]: None,
                    POLL_FIELDS["extension"]: generated["extension"],
                    POLL_FIELDS["imageBytes"]: payload,
                    POLL_FIELDS["providerOutputIdentity"]: duplicate_id,
                    POLL_FIELDS["outputAssets"]: [
                        {
                            ASSET_FIELDS["providerOutputIdentity"]: duplicate_id,
                            ASSET_FIELDS["sha256"]: payload_sha,
                        },
                        {
                            ASSET_FIELDS["providerOutputIdentity"]: duplicate_id,
                            ASSET_FIELDS["sha256"]: payload_sha,
                        },
                    ],
                }
            ],
        )
        option_field = CONTRACT["requestOptionsField"]
        option_fields = CONTRACT["requestOptionFields"]

        result = self.run_case(
            item_id,
            adapters,
            **{
                option_field: {
                    option_fields["imageCount"]: 2,
                    option_fields["primaryOutputIndex"]: 0,
                }
            },
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_retryable_poll_failure_resumes_same_provider_request_without_resubmit(self) -> None:
        item_id = "queued-generation-retry-resume"
        first_adapters = self.adapters_for(
            item_id,
            poll_results=[failed_poll("retryable", "provider status timeout")],
        )

        first = self.run_case(item_id, first_adapters)
        first_wal = load_json(first.output_dir / "generation-wal.json")
        provider_request_id = first_wal[WAL_FIELDS["providerRequestIdentity"]]
        second_adapters = self.adapters_for(item_id)
        second = self.run_case(item_id, second_adapters)

        self.assertEqual(RULES["resultStates"]["failed"], first.state)
        self.assertEqual(RULES["errorCodes"]["generationRetryable"], first.error_code)
        self.assertEqual(RULES["resultStates"]["completed"], second.state)
        self.assertTrue(second.resumed)
        self.assertEqual([], second_adapters.submission_calls)
        self.assertEqual(provider_request_id, second_adapters.poll_calls[0]["providerRequestId"])

    def test_process_death_during_poll_cannot_exceed_the_retry_budget(self) -> None:
        item_id = "queued-generation-poll-process-death-budget"
        retry_budget = CONTRACT["retryBudgets"]["retryable"]
        for attempt in range(retry_budget):
            adapters = self.adapters_for(item_id)

            def process_death(*_args) -> dict:
                raise SystemExit("simulated process death during provider poll")

            adapters.poll_generation = process_death  # type: ignore[method-assign]
            with self.assertRaises(SystemExit):
                self.run_case(item_id, adapters)
            self.assertEqual(1 if attempt == 0 else 0, len(adapters.submission_calls))
            self.assertEqual(
                attempt + 1,
                load_json(self.output_root / item_id / "generation-wal.json")[
                    WAL_FIELDS["pollAttemptCount"]
                ],
            )

        final_adapters = self.adapters_for(item_id)
        final = self.run_case(item_id, final_adapters)

        self.assertEqual(RULES["resultStates"]["failed"], final.state)
        self.assertEqual(
            RULES["errorCodes"]["generationPermanentFailure"], final.error_code
        )
        self.assertEqual([], final_adapters.submission_calls)
        self.assertEqual([], final_adapters.poll_calls)
        self.assertEqual(
            retry_budget,
            load_json(final.output_dir / "generation-wal.json")[
                WAL_FIELDS["pollAttemptCount"]
            ],
        )

    def test_succeeded_wal_reuses_local_candidate_without_polling_provider_again(self) -> None:
        item_id = "queued-generation-resume-visual-review"
        first_adapters = self.adapters_for(item_id)

        def interrupted_review(*_args) -> dict:
            raise ConnectionError("visual review process interrupted")

        first_adapters.inspect_generated = interrupted_review  # type: ignore[method-assign]
        first = self.run_case(item_id, first_adapters)
        first_wal = load_json(first.output_dir / "generation-wal.json")
        second_adapters = self.adapters_for(
            item_id,
            poll_results=[failed_poll("permanent", "provider result expired")],
        )

        second = self.run_case(item_id, second_adapters)

        self.assertEqual(RULES["resultStates"]["failed"], first.state)
        self.assertEqual(
            CONTRACT["walStatuses"]["succeeded"], first_wal[WAL_FIELDS["status"]]
        )
        self.assertEqual(RULES["resultStates"]["completed"], second.state)
        self.assertTrue(second.resumed)
        self.assertEqual([], second_adapters.submission_calls)
        self.assertEqual([], second_adapters.poll_calls)

    def test_retryable_wal_requires_the_provider_request_identity_for_recovery(self) -> None:
        item_id = "queued-generation-corrupt-retry-wal"
        first = self.run_case(
            item_id,
            self.adapters_for(
                item_id,
                poll_results=[failed_poll("retryable", "provider status timeout")],
            ),
        )
        wal = load_json(first.output_dir / "generation-wal.json")
        wal[WAL_FIELDS["providerRequestIdentity"]] = None
        self.rewrite_tracked_json(first.output_dir, "generation-wal.json", wal)

        resumed = self.run_case(item_id, self.adapters_for(item_id))

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            resumed.error_code,
        )

    def test_valid_wal_forward_repairs_a_manifest_digest_left_by_process_death(self) -> None:
        item_id = "queued-generation-wal-forward-repair"
        first = self.run_case(
            item_id,
            self.adapters_for(
                item_id,
                poll_results=[failed_poll("retryable", "provider status timeout")],
            ),
        )
        wal = load_json(first.output_dir / "generation-wal.json")
        manifest_path = first.output_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        manifest["artifacts"]["generation-wal.json"]["sha256"] = wal[
            WAL_FIELDS["previousWalSha256"]
        ]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        second_adapters = self.adapters_for(item_id)

        second = self.run_case(item_id, second_adapters)

        self.assertEqual(RULES["resultStates"]["completed"], second.state)
        self.assertEqual([], second_adapters.submission_calls)
        self.assertEqual(1, len(second_adapters.poll_calls))

    def test_pre_submit_staging_is_adopted_after_manifest_write_is_interrupted(self) -> None:
        for keep_prepared_wal in (False, True):
            with self.subTest(keep_prepared_wal=keep_prepared_wal):
                item_id = (
                    "queued-generation-pre-submit-adopt-with-wal"
                    if keep_prepared_wal
                    else "queued-generation-pre-submit-adopt-task-only"
                )
                first = self.run_case(
                    item_id,
                    self.adapters_for(
                        item_id,
                        poll_results=[
                            failed_poll("retryable", "provider status timeout")
                        ],
                    ),
                )
                wal_path = first.output_dir / "generation-wal.json"
                wal = load_json(wal_path)
                for role in (
                    "provider",
                    "model",
                    "providerRequestIdentity",
                    "providerOutputIdentity",
                    "outputSha256",
                    "failureClass",
                    "failureReason",
                ):
                    wal[WAL_FIELDS[role]] = None
                wal[WAL_FIELDS["status"]] = CONTRACT["walStatuses"]["prepared"]
                wal[WAL_FIELDS["previousWalSha256"]] = None
                wal[WAL_FIELDS["outputAssets"]] = []
                wal[WAL_FIELDS["pollAttemptCount"]] = 0
                wal_path.write_text(
                    json.dumps(wal, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if not keep_prepared_wal:
                    wal_path.unlink()
                manifest_path = first.output_dir / "production-manifest.json"
                manifest = load_json(manifest_path)
                for name in (
                    "generation-task.json",
                    "generation-wal.json",
                ):
                    manifest["artifacts"].pop(name)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                resumed_adapters = self.adapters_for(item_id)

                resumed = self.run_case(item_id, resumed_adapters)

                self.assertEqual(RULES["resultStates"]["completed"], resumed.state)
                self.assertEqual(1, len(resumed_adapters.submission_calls))
                self.assertEqual(1, len(resumed_adapters.poll_calls))

    def test_succeeded_wal_forward_repair_adopts_the_local_candidate(self) -> None:
        item_id = "queued-generation-succeeded-forward-repair"
        first_adapters = self.adapters_for(item_id)

        def interrupted_review(*_args) -> dict:
            raise ConnectionError("visual review process interrupted")

        first_adapters.inspect_generated = interrupted_review  # type: ignore[method-assign]
        first = self.run_case(item_id, first_adapters)
        wal = load_json(first.output_dir / "generation-wal.json")
        manifest_path = first.output_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        manifest["artifacts"]["generation-wal.json"]["sha256"] = wal[
            WAL_FIELDS["previousWalSha256"]
        ]
        manifest["artifacts"].pop("evidence/generated-candidate-image.png")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        resumed_adapters = self.adapters_for(
            item_id,
            poll_results=[failed_poll("permanent", "provider result expired")],
        )

        resumed = self.run_case(item_id, resumed_adapters)

        self.assertEqual(RULES["resultStates"]["completed"], resumed.state)
        self.assertEqual([], resumed_adapters.submission_calls)
        self.assertEqual([], resumed_adapters.poll_calls)

    def test_non_prepared_wal_requires_a_previous_digest(self) -> None:
        for previous_digest in (None, "junk"):
            with self.subTest(previous_digest=previous_digest):
                item_id = (
                    "queued-generation-wal-chain-none"
                    if previous_digest is None
                    else "queued-generation-wal-chain-junk"
                )
                first = self.run_case(item_id, self.adapters_for(item_id))
                wal = load_json(first.output_dir / "generation-wal.json")
                wal[WAL_FIELDS["previousWalSha256"]] = previous_digest
                self.rewrite_tracked_json(
                    first.output_dir, "generation-wal.json", wal
                )

                resumed = self.run_case(item_id, self.adapters_for(item_id))

                self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)

    def test_active_recovery_rejects_malformed_generation_package_stably(self) -> None:
        item_id = "queued-generation-malformed-active-package"
        first = self.run_case(
            item_id,
            self.adapters_for(
                item_id,
                poll_results=[failed_poll("retryable", "provider status timeout")],
            ),
        )
        self.rewrite_tracked_json(first.output_dir, "generation-package.json", [])
        resumed_adapters = self.adapters_for(item_id)

        resumed = self.run_case(item_id, resumed_adapters)

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            resumed.error_code,
        )
        self.assertEqual([], resumed_adapters.submission_calls)
        self.assertEqual([], resumed_adapters.poll_calls)

    def test_completed_wal_poll_count_must_be_reachable(self) -> None:
        for count in (0, CONTRACT["retryBudgets"]["retryable"] + 1):
            with self.subTest(count=count):
                item_id = f"queued-generation-impossible-poll-count-{count}"
                first = self.run_case(item_id, self.adapters_for(item_id))
                wal = load_json(first.output_dir / "generation-wal.json")
                wal[WAL_FIELDS["pollAttemptCount"]] = count
                self.rewrite_tracked_json(first.output_dir, "generation-wal.json", wal)

                resumed = self.run_case(item_id, self.adapters_for(item_id))

                self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)

    def test_uncertain_submit_is_not_duplicated_on_rerun(self) -> None:
        item_id = "queued-generation-submit-unknown"
        first_adapters = self.adapters_for(
            item_id,
            submit_exception=TimeoutError("connection closed after submit"),
        )

        first = self.run_case(item_id, first_adapters)
        second_adapters = self.adapters_for(item_id)
        second = self.run_case(item_id, second_adapters)

        self.assertEqual(RULES["errorCodes"]["generationSubmissionUnknown"], first.error_code)
        self.assertEqual(RULES["resultStates"]["needs_input"], second.state)
        self.assertEqual(RULES["errorCodes"]["generationSubmissionUnknown"], second.error_code)
        self.assertEqual([], second_adapters.submission_calls)
        self.assertEqual([], second_adapters.poll_calls)

    def test_retry_budget_is_persisted_and_exhaustion_becomes_permanent(self) -> None:
        item_id = "queued-generation-retry-budget"
        results = []
        for _attempt in range(CONTRACT["retryBudgets"]["retryable"]):
            adapters = self.adapters_for(
                item_id,
                poll_results=[failed_poll("retryable", "temporary queue failure")],
            )
            results.append(self.run_case(item_id, adapters))

        final_wal = load_json(self.output_root / item_id / "generation-wal.json")
        after_budget = self.adapters_for(item_id)
        repeated = self.run_case(item_id, after_budget)

        self.assertEqual(
            RULES["errorCodes"]["generationPermanentFailure"],
            results[-1].error_code,
        )
        self.assertEqual(
            CONTRACT["retryBudgets"]["retryable"],
            final_wal[WAL_FIELDS["pollAttemptCount"]],
        )
        self.assertEqual(RULES["errorCodes"]["generationPermanentFailure"], repeated.error_code)
        self.assertEqual([], after_budget.submission_calls)
        self.assertEqual([], after_budget.poll_calls)

    def test_failure_classes_route_to_stable_results_before_visual_review(self) -> None:
        cases = {
            "replanRequired": ("blocked", "generationReplanRequired"),
            "humanReview": ("needs_input", "riskNeedsReview"),
            "permanent": ("failed", "generationPermanentFailure"),
        }
        for failure_role, (result_role, error_role) in cases.items():
            with self.subTest(failure_role=failure_role):
                item_id = f"queued-generation-{failure_role.lower()}"
                adapters = self.adapters_for(
                    item_id,
                    poll_results=[failed_poll(failure_role, f"{failure_role} evidence")],
                )

                result = self.run_case(item_id, adapters)

                self.assertEqual(RULES["resultStates"][result_role], result.state)
                self.assertEqual(RULES["errorCodes"][error_role], result.error_code)
                self.assertFalse((result.output_dir / "visual-review.json").exists())
                self.assertEqual([], adapters.upload_calls)

    def test_visual_hard_failure_redo_creates_a_new_frozen_provider_task(self) -> None:
        item_id = "queued-generation-visual-redo"
        gate = RULES["visualReviewContract"]["hardGateRoles"]["dependencyClosure"]
        first_adapters = self.adapters_for(item_id).with_visual_review(
            {"hardGates": {gate: False}}
        )
        first_adapters.expected_output_dir = self.output_root / item_id

        first = self.run_case(item_id, first_adapters)
        unapproved_adapters = self.adapters_for(item_id)
        unapproved = self.run_case(item_id, unapproved_adapters)
        second_adapters = self.adapters_for(item_id)
        second = run_production(
            {
                **self.request,
                "productionItemId": item_id,
                "authorizeVisualRedo": True,
            },
            self.output_root,
            second_adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["errorCodes"]["visualHardFailure"], first.error_code)
        self.assertEqual(RULES["errorCodes"]["visualHardFailure"], unapproved.error_code)
        self.assertTrue(unapproved.resumed)
        self.assertEqual([], unapproved_adapters.submission_calls)
        self.assertEqual(RULES["resultStates"]["completed"], second.state)
        first_task = load_json(second.output_dir / "generation-task.json")
        second_task = load_json(second.output_dir / "generation-task-r2.json")
        self.assertNotEqual(
            first_task[TASK_FIELDS["taskIdentity"]],
            second_task[TASK_FIELDS["taskIdentity"]],
        )
        self.assertTrue((second.output_dir / "generation-wal-r2.json").is_file())
        self.assertEqual(1, len(second_adapters.submission_calls))

    def test_real_fal_adapter_runs_submit_status_result_and_download_through_public_flow(self) -> None:
        class Completed:
            pass

        class Handle:
            request_id = "fal-provider-request-001"

        class FakeFalClient:
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
                return {
                    "images": [
                        {
                            "url": (
                                "https://fal.example/output-001.png"
                                "?token=TOPSECRET&signature=ABC"
                            )
                        }
                    ]
                }

        item_id = "real-fal-adapter-success"
        client = FakeFalClient()
        delegate = DeterministicFixtureAdapters(FIXTURE)
        adapters = FalQueueWorkflowAdapters(
            delegate,
            client=client,
            download_bytes=lambda _url: DeterministicFixtureAdapters._fixture_image_result(
                FIXTURE / "approved-template-image.ppm"
            )["imageBytes"],
            sleep=lambda _seconds: None,
        )

        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        self.assertEqual(CONTRACT["fal"]["model"], client.submit_calls[0][0])
        submitted_arguments = client.submit_calls[0][1]
        self.assertEqual(CONTRACT["defaultImageCount"], submitted_arguments["num_images"])
        self.assertTrue(submitted_arguments["image_urls"][0].startswith("data:image/"))
        self.assertEqual(
            [(CONTRACT["fal"]["model"], Handle.request_id)],
            client.status_calls,
        )
        self.assertEqual(client.status_calls, client.result_calls)
        wal = load_json(result.output_dir / "generation-wal.json")
        self.assertEqual(CONTRACT["providerRoles"]["fal"], wal[WAL_FIELDS["provider"]])
        persisted_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                result.output_dir / "generation-task.json",
                result.output_dir / "generation-wal.json",
            )
        )
        self.assertNotIn("data:image/", persisted_text)
        self.assertNotIn("TOPSECRET", persisted_text)
        self.assertNotIn("signature=ABC", persisted_text)

    def test_real_fal_adapter_regenerates_pure_multi_target_content_from_contract(self) -> None:
        class Handle:
            request_id = "fal-content-regeneration-001"

        class FakeFalClient:
            def __init__(self) -> None:
                self.submit_calls: list[tuple[str, dict]] = []

            def submit(self, model: str, *, arguments: dict) -> Handle:
                self.submit_calls.append((model, arguments))
                return Handle()

        runtime = RULES["runtimeSemanticsContract"]
        runtime_fields = runtime["fields"]
        binding_fields = runtime["inputBindingFields"]
        client = FakeFalClient()
        adapters = FalQueueWorkflowAdapters(
            DeterministicFixtureAdapters(FIXTURE), client=client
        )
        package = {
            "runtimeSemantics": {
                runtime_fields["inputBindings"]: {
                    "panel_subjects": {
                        binding_fields["operation"]: runtime["operations"][
                            "replaceContent"
                        ],
                        binding_fields["targetIdentities"]: [
                            "panel-a",
                            "panel-b",
                            "panel-c",
                            "panel-d",
                        ],
                    }
                }
            }
        }
        task = {
            TASK_FIELDS["requestIntent"]: {
                INTENT_FIELDS["prompt"]: "四位球队成员分别位于四格旧相片中。",
                INTENT_FIELDS["imageSize"]: "1024x1024",
                INTENT_FIELDS["imageCount"]: 1,
                INTENT_FIELDS["outputFormat"]: "png",
            }
        }

        submission = adapters.submit_generation(
            FIXTURE / "source-image.ppm", package, task
        )

        self.assertEqual(1, len(client.submit_calls))
        model, arguments = client.submit_calls[0]
        self.assertEqual(CONTRACT["fal"]["contentRegenerationModel"], model)
        self.assertNotIn("image_urls", arguments)
        self.assertEqual(model, submission[SUBMISSION_FIELDS["model"]])

    def test_real_fal_adapter_resumes_a_submitted_request_without_resubmit(self) -> None:
        class Completed:
            pass

        class Handle:
            request_id = "fal-provider-request-recovery-001"

        class InterruptedClient:
            def __init__(self) -> None:
                self.submit_calls = 0

            def submit(self, _model: str, *, arguments: dict) -> Handle:
                self.submit_calls += 1
                return Handle()

            def status(self, _model: str, _request_id: str) -> object:
                raise TimeoutError("status connection interrupted")

        class RecoveryClient:
            def __init__(self) -> None:
                self.submit_calls = 0
                self.status_request_ids: list[str] = []

            def submit(self, _model: str, *, arguments: dict) -> object:
                self.submit_calls += 1
                raise AssertionError("recovery must not submit again")

            def status(self, _model: str, request_id: str) -> Completed:
                self.status_request_ids.append(request_id)
                return Completed()

            def result(self, _model: str, _request_id: str) -> dict:
                return {"images": [{"url": "https://fal.example/recovered.png"}]}

        item_id = "real-fal-adapter-request-recovery"
        first_client = InterruptedClient()
        first = self.run_case(
            item_id,
            FalQueueWorkflowAdapters(
                DeterministicFixtureAdapters(FIXTURE),
                client=first_client,
                sleep=lambda _seconds: None,
            ),
        )
        recovery_client = RecoveryClient()
        second = self.run_case(
            item_id,
            FalQueueWorkflowAdapters(
                DeterministicFixtureAdapters(FIXTURE),
                client=recovery_client,
                download_bytes=lambda _url: DeterministicFixtureAdapters._fixture_image_result(
                    FIXTURE / "approved-template-image.ppm"
                )["imageBytes"],
                sleep=lambda _seconds: None,
            ),
        )

        self.assertEqual(RULES["errorCodes"]["generationRetryable"], first.error_code)
        self.assertEqual(RULES["resultStates"]["completed"], second.state)
        self.assertEqual(1, first_client.submit_calls)
        self.assertEqual(0, recovery_client.submit_calls)
        self.assertEqual([Handle.request_id], recovery_client.status_request_ids)

    def test_real_fal_adapter_hard_gate_redo_submits_a_new_revision(self) -> None:
        class Completed:
            pass

        class Handle:
            def __init__(self, request_id: str) -> None:
                self.request_id = request_id

        class SuccessfulClient:
            def __init__(self, request_id: str) -> None:
                self.request_id = request_id
                self.submit_calls = 0

            def submit(self, _model: str, *, arguments: dict) -> Handle:
                self.submit_calls += 1
                return Handle(self.request_id)

            def status(self, _model: str, _request_id: str) -> Completed:
                return Completed()

            def result(self, _model: str, _request_id: str) -> dict:
                return {"images": [{"url": "https://fal.example/revision.png"}]}

        image_bytes = DeterministicFixtureAdapters._fixture_image_result(
            FIXTURE / "approved-template-image.ppm"
        )["imageBytes"]
        item_id = "real-fal-adapter-visual-redo"
        visual_contract = RULES["visualReviewContract"]
        gate = visual_contract["hardGateRoles"][
            "dependencyClosure"
        ]
        first_client = SuccessfulClient("fal-provider-request-redo-r1")
        first = self.run_case(
            item_id,
            FalQueueWorkflowAdapters(
                DeterministicFixtureAdapters(FIXTURE).with_visual_review(
                    {
                        visual_contract["evidenceFieldRoles"]["hardGates"]: {
                            gate: False
                        }
                    }
                ),
                client=first_client,
                download_bytes=lambda _url: image_bytes,
                sleep=lambda _seconds: None,
            ),
        )
        second_client = SuccessfulClient("fal-provider-request-redo-r2")
        second = run_production(
            {
                **self.request,
                "productionItemId": item_id,
                "authorizeVisualRedo": True,
            },
            self.output_root,
            FalQueueWorkflowAdapters(
                DeterministicFixtureAdapters(FIXTURE),
                client=second_client,
                download_bytes=lambda _url: image_bytes,
                sleep=lambda _seconds: None,
            ),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["errorCodes"]["visualHardFailure"], first.error_code)
        self.assertEqual(RULES["resultStates"]["completed"], second.state)
        self.assertEqual(1, first_client.submit_calls)
        self.assertEqual(1, second_client.submit_calls)
        first_task = load_json(second.output_dir / "generation-task.json")
        second_task = load_json(second.output_dir / "generation-task-r2.json")
        self.assertNotEqual(
            first_task[TASK_FIELDS["taskIdentity"]],
            second_task[TASK_FIELDS["taskIdentity"]],
        )

    def test_provider_failure_details_are_sanitized_before_any_persistence(self) -> None:
        class FakeFalClient:
            def submit(self, _model: str, *, arguments: dict) -> object:
                raise ValueError(
                    "provider echoed data:image/png;base64,TOPSECRET "
                    "Authorization: Bearer OTHERSECRET password=TOPPASSWORD "
                    "credential=TOPCREDENTIAL /root/private.png /tmp/source.png"
                )

        item_id = "real-fal-adapter-sanitized-failure"
        adapters = FalQueueWorkflowAdapters(
            DeterministicFixtureAdapters(FIXTURE),
            client=FakeFalClient(),
            sleep=lambda _seconds: None,
        )

        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        persisted_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in result.output_dir.rglob("*.json")
        )
        for secret in (
            "data:image/",
            "TOPSECRET",
            "OTHERSECRET",
            "TOPPASSWORD",
            "TOPCREDENTIAL",
            "/root/private.png",
            "/tmp/source.png",
        ):
            self.assertNotIn(secret, persisted_text)

    def test_poll_exception_details_and_unsafe_provider_ids_never_persist(self) -> None:
        cases: list[tuple[str, object]] = []

        poll_exception = self.adapters_for("queued-generation-poll-secret")

        def secret_poll(*_args) -> dict:
            raise RuntimeError("Authorization: Bearer POLLSECRET /tmp/poll.png")

        poll_exception.poll_generation = secret_poll  # type: ignore[method-assign]
        cases.append(("queued-generation-poll-secret", poll_exception))

        unsafe_request = self.adapters_for("queued-generation-request-secret")
        original_submit = unsafe_request.submit_generation

        def signed_request_id(*args) -> dict:
            result = original_submit(*args)
            result[SUBMISSION_FIELDS["providerRequestIdentity"]] = (
                "https://provider.test/request?token=REQUESTSECRET"
            )
            return result

        unsafe_request.submit_generation = signed_request_id  # type: ignore[method-assign]
        cases.append(("queued-generation-request-secret", unsafe_request))

        for item_id, adapters in cases:
            with self.subTest(item_id=item_id):
                result = self.run_case(item_id, adapters)
                persisted_text = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in result.output_dir.rglob("*.json")
                )
                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                for secret in (
                    "POLLSECRET",
                    "/tmp/poll.png",
                    "REQUESTSECRET",
                    "?token=",
                ):
                    self.assertNotIn(secret, persisted_text)

    def test_candidate_bytes_must_decode_as_the_frozen_single_image_format(self) -> None:
        def png_chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        truncated_png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(b"x"))
            + png_chunk(b"IEND", b"")
        )
        for name, payload in (
            ("html", b"<!doctype html><title>provider error</title>"),
            ("truncated-png", truncated_png),
        ):
            with self.subTest(name=name):
                item_id = f"queued-generation-invalid-image-{name}"
                payload_sha = hashlib.sha256(payload).hexdigest()
                output_id = f"invalid-image-{name}"
                adapters = self.adapters_for(
                    item_id,
                    poll_results=[
                        {
                            POLL_FIELDS["status"]: CONTRACT["pollStatuses"]["succeeded"],
                            POLL_FIELDS["failureClass"]: None,
                            POLL_FIELDS["failureReason"]: None,
                            POLL_FIELDS["extension"]: CONTRACT[
                                "outputFormatExtensions"
                            ]["png"],
                            POLL_FIELDS["imageBytes"]: payload,
                            POLL_FIELDS["providerOutputIdentity"]: output_id,
                            POLL_FIELDS["outputAssets"]: [
                                {
                                    ASSET_FIELDS["providerOutputIdentity"]: output_id,
                                    ASSET_FIELDS["sha256"]: payload_sha,
                                }
                            ],
                        }
                    ],
                )

                result = self.run_case(item_id, adapters)

                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual([], adapters.upload_calls)

    def test_real_fal_adapter_rejects_malformed_https_output_before_download(self) -> None:
        class Completed:
            pass

        class Handle:
            request_id = "fal-provider-request-invalid-url"

        class FakeFalClient:
            def submit(self, _model: str, *, arguments: dict) -> Handle:
                return Handle()

            def status(self, _model: str, _request_id: str) -> Completed:
                return Completed()

            def result(self, _model: str, _request_id: str) -> dict:
                return {"images": [{"url": "https://fal.example:bad/output.png"}]}

        item_id = "real-fal-adapter-invalid-url"
        downloads: list[str] = []
        adapters = FalQueueWorkflowAdapters(
            DeterministicFixtureAdapters(FIXTURE),
            client=FakeFalClient(),
            download_bytes=lambda url: downloads.append(url) or b"unexpected",
            sleep=lambda _seconds: None,
        )

        result = self.run_case(item_id, adapters)

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(
            RULES["errorCodes"]["generationPermanentFailure"], result.error_code
        )
        self.assertEqual([], downloads)
        self.assertEqual([], adapters.upload_calls)

    def test_real_fal_download_rejects_private_targets_and_redirect_downgrades(self) -> None:
        class Completed:
            pass

        class Handle:
            request_id = "fal-provider-request-fetch-policy"

        class FakeResponse:
            def __init__(self, url: str, status: int, location: str | None = None):
                self._url = url
                self.status = status
                self.headers = {} if location is None else {"Location": location}

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def geturl(self) -> str:
                return self._url

            def read(self) -> bytes:
                return b"unexpected"

        class FakeFalClient:
            def __init__(self, url: str) -> None:
                self.url = url

            def submit(self, _model: str, *, arguments: dict) -> Handle:
                return Handle()

            def status(self, _model: str, _request_id: str) -> Completed:
                return Completed()

            def result(self, _model: str, _request_id: str) -> dict:
                return {"images": [{"url": self.url}]}

        cases = (
            (
                "private-literal",
                "https://127.0.0.1/output.png",
                None,
                "93.184.216.34",
            ),
            (
                "redirect-downgrade",
                "https://public.example/output.png",
                "http://169.254.169.254/latest/meta-data",
                "93.184.216.34",
            ),
            (
                "dns-rebinding-peer",
                "https://public.example/output.png",
                None,
                "127.0.0.1",
            ),
        )
        for suffix, initial_url, redirect_url, peer_ip in cases:
            with self.subTest(suffix=suffix):
                open_calls: list[str] = []

                def open_url(request, *, timeout: int) -> FakeResponse:
                    open_calls.append(request.full_url)
                    return FakeResponse(
                        request.full_url,
                        302 if redirect_url is not None else 200,
                        redirect_url,
                    )

                def resolve_host(host: str, *_args, **_kwargs) -> list[tuple]:
                    return [(None, None, None, None, ("93.184.216.34", 0))]

                item_id = f"real-fal-fetch-policy-{suffix}"
                adapters = FalQueueWorkflowAdapters(
                    DeterministicFixtureAdapters(FIXTURE),
                    client=FakeFalClient(initial_url),
                    open_url=open_url,
                    resolve_host=resolve_host,
                    peer_address=lambda _response: peer_ip,
                    sleep=lambda _seconds: None,
                )

                result = self.run_case(item_id, adapters)

                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual([], adapters.upload_calls)
                self.assertEqual(
                    [] if suffix == "private-literal" else [initial_url], open_calls
                )

    def test_completed_item_rejects_wal_output_digest_even_when_manifest_is_rehashed(self) -> None:
        item_id = "queued-generation-forged-wal"
        first = self.run_case(item_id, self.adapters_for(item_id))
        wal = load_json(first.output_dir / "generation-wal.json")
        wal[WAL_FIELDS["outputSha256"]] = "0" * 64
        self.rewrite_tracked_json(first.output_dir, "generation-wal.json", wal)

        resumed = self.run_case(item_id, self.adapters_for(item_id))

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            resumed.error_code,
        )

    def test_completed_item_rejects_malformed_task_even_when_manifest_is_rehashed(self) -> None:
        item_id = "queued-generation-malformed-task"
        first = self.run_case(item_id, self.adapters_for(item_id))
        task = load_json(first.output_dir / "generation-task.json")
        task[TASK_FIELDS["requestIntent"]] = "malformed"
        self.rewrite_tracked_json(first.output_dir, "generation-task.json", task)

        resumed = self.run_case(item_id, self.adapters_for(item_id))

        self.assertEqual(RULES["resultStates"]["blocked"], resumed.state)
        self.assertEqual(
            RULES["errorCodes"]["productionItemIntegrityFailure"],
            resumed.error_code,
        )


if __name__ == "__main__":
    unittest.main()
