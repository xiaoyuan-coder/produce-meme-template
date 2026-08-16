from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from scripts.produce_meme_template import DeterministicFixtureAdapters, run_production


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "e2e" / "simple-animal"
FIXED_TIME = datetime.fromisoformat("2026-08-16T08:00:00+00:00")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


RULES = load_json(ROOT / "contracts" / "machine-rules.json")


class Issue4TemplateImageGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name)
        self.request = load_json(FIXTURE / "request.json")
        self.request["sourceImage"] = str(FIXTURE / self.request["sourceImage"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_clear_pass_binds_every_gate_and_is_autonomously_approved(self) -> None:
        contract = RULES["visualReviewContract"]
        result = run_production(
            {**self.request, "productionItemId": "complete-visual-gate"},
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["completed"], result.state)
        review = load_json(result.output_dir / "visual-review.json")
        package = load_json(result.output_dir / "generation-package.json")
        candidate = result.output_dir / "evidence" / "generated-candidate-image.ppm"
        approved = result.output_dir / "evidence" / "approved-template-image.ppm"
        evidence_payload = {
            field: review[field]
            for field in contract["evidenceFieldRoles"].values()
        }
        evidence_fields = contract["evidenceFieldRoles"]
        self.assertEqual(
            set(contract["hardGateRoles"].values()), set(review[evidence_fields["hardGates"]])
        )
        self.assertEqual(
            set(RULES["visualDimensions"]), set(review[evidence_fields["visualDimensions"]])
        )
        self.assertEqual(
            set(contract["cleanlinessFindingRoles"].values()),
            set(review[evidence_fields["cleanliness"]]),
        )
        self.assertEqual(contract["decisionValues"]["approved"], review["decision"])
        self.assertEqual(hashlib.sha256(candidate.read_bytes()).hexdigest(), review["bindings"]["generatedImageSha256"])
        self.assertEqual(canonical_sha(package), review["bindings"]["generationPackageSha256"])
        self.assertEqual(canonical_sha(evidence_payload), review["bindings"]["evidenceSha256"])
        self.assertTrue(review["method"]["id"])
        self.assertTrue(review["method"]["version"])
        self.assertEqual(candidate.read_bytes(), approved.read_bytes())

    def test_hard_failures_cannot_be_overridden_or_become_approved_images(self) -> None:
        contract = RULES["visualReviewContract"]
        hard_gates = contract["hardGateRoles"]
        cleanliness = contract["cleanlinessFindingRoles"]
        scenarios = {
            "missed-repeated-instance": {
                "hardGates": {hard_gates["dependencyClosure"]: False},
            },
            "non-target-text-drift": {
                "hardGates": {
                    hard_gates["nonTargetPreservation"]: False,
                    hard_gates["visibleText"]: False,
                },
                "visibleTextEvidence": {"pass": False, "evidence": "非目标文字发生未授权改写"},
            },
            "style-drift": {
                "visualDimensions": {"medium": {"pass": False, "evidence": "摄影媒介漂移为插画"}},
            },
            "ghost-text": {
                "hardGates": {hard_gates["fullCanvasCleanliness"]: False},
                "cleanlinessFindings": {cleanliness["ghostText"]: True},
            },
            "pseudo-signature": {
                "hardGates": {hard_gates["fullCanvasCleanliness"]: False},
                "cleanlinessFindings": {cleanliness["pseudoSignature"]: True},
            },
            "platform-watermark": {
                "hardGates": {
                    hard_gates["fullCanvasCleanliness"]: False,
                    hard_gates["watermarkAbsence"]: False,
                },
                "cleanlinessFindings": {cleanliness["platformWatermark"]: True},
            },
        }

        for item_id, overrides in scenarios.items():
            with self.subTest(item_id=item_id):
                adapters = DeterministicFixtureAdapters(FIXTURE).with_visual_review(overrides)
                request = {
                    **self.request,
                    "productionItemId": item_id,
                    "reviewDecision": contract["decisionValues"]["approved"],
                }
                result = run_production(
                    request,
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(RULES["errorCodes"]["visualHardFailure"], result.error_code)
                review = load_json(result.output_dir / "visual-review.json")
                self.assertEqual(contract["decisionValues"]["rejected"], review["decision"])
                self.assertTrue((result.output_dir / "evidence" / "generated-candidate-image.ppm").is_file())
                self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
                self.assertFalse((result.output_dir / "template-analysis.json").exists())
                self.assertEqual([], adapters.upload_calls)

    def test_ambiguity_and_evidence_risk_require_review_without_approving_the_image(self) -> None:
        contract = RULES["visualReviewContract"]
        signal = contract["ambiguitySignalRoles"]["evidenceInsufficient"]
        adapters = DeterministicFixtureAdapters(FIXTURE).with_visual_review(
            {"ambiguitySignals": {signal: True}}
        )
        request = {
            **self.request,
            "productionItemId": "visual-evidence-needs-review",
            "reviewDecision": contract["decisionValues"]["approved"],
        }

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RULES["resultStates"]["needs_input"], result.state)
        self.assertEqual(RULES["errorCodes"]["riskNeedsReview"], result.error_code)
        review = load_json(result.output_dir / "visual-review.json")
        self.assertEqual(contract["decisionValues"]["needsReview"], review["decision"])
        self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_stale_visual_review_binding_fails_before_approval_and_downstream_analysis(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        inspect = adapters.inspect_generated

        def stale_review(generated_image: Path, review_context: dict[str, str]) -> dict:
            review = inspect(generated_image, review_context)
            review["bindings"]["generationPackageSha256"] = "0" * 64
            return review

        adapters.inspect_generated = stale_review
        result = run_production(
            {**self.request, "productionItemId": "stale-visual-review-binding"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
        self.assertFalse((result.output_dir / "template-analysis.json").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_non_object_visual_review_is_a_stable_external_failure(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        adapters.inspect_generated = lambda *_args: []

        result = run_production(
            {**self.request, "productionItemId": "invalid-visual-review-shape"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertEqual([], adapters.upload_calls)

    def test_adapters_cannot_mutate_generation_or_review_binding_facts(self) -> None:
        generation_adapters = DeterministicFixtureAdapters(FIXTURE)
        generate = generation_adapters.generate

        def mutate_generation(_source_image: Path, generation_package: dict) -> dict:
            generation_package["requestId"] = "adapter-mutated-request"
            return generate(_source_image, generation_package)

        generation_adapters.generate = mutate_generation
        generation_result = run_production(
            {**self.request, "productionItemId": "mutated-generation-package"},
            self.output_root,
            generation_adapters,
            clock=lambda: FIXED_TIME,
        )

        review_adapters = DeterministicFixtureAdapters(FIXTURE)
        inspect = review_adapters.inspect_generated

        def mutate_review_context(generated_image: Path, review_context: dict[str, str]) -> dict:
            review_context["generationPackageSha256"] = "0" * 64
            return inspect(generated_image, review_context)

        review_adapters.inspect_generated = mutate_review_context
        review_result = run_production(
            {**self.request, "productionItemId": "mutated-review-binding"},
            self.output_root,
            review_adapters,
            clock=lambda: FIXED_TIME,
        )

        package_adapters = DeterministicFixtureAdapters(FIXTURE)
        generate_with_same_request = package_adapters.generate

        def mutate_generation_sections(source_image: Path, generation_package: dict) -> dict:
            generation_package["sections"]["replacementTarget"] = "adapter changed the task"
            return generate_with_same_request(source_image, generation_package)

        package_adapters.generate = mutate_generation_sections
        package_result = run_production(
            {**self.request, "productionItemId": "mutated-generation-sections"},
            self.output_root,
            package_adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], generation_result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], generation_result.error_code)
        self.assertEqual(RULES["resultStates"]["failed"], review_result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], review_result.error_code)
        self.assertEqual(RULES["resultStates"]["failed"], package_result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], package_result.error_code)
        self.assertEqual([], generation_adapters.upload_calls)
        self.assertEqual([], review_adapters.upload_calls)
        self.assertEqual([], package_adapters.upload_calls)

    def test_visual_review_cannot_change_candidate_bytes_after_binding_them(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        inspect = adapters.inspect_generated

        def mutate_candidate(generated_image: Path, review_context: dict[str, str]) -> dict:
            review = inspect(generated_image, review_context)
            generated_image.write_bytes(generated_image.read_bytes() + b"changed-after-review")
            return review

        adapters.inspect_generated = mutate_candidate

        result = run_production(
            {**self.request, "productionItemId": "mutated-candidate-after-review"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "evidence" / "approved-template-image.ppm").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_template_analysis_cannot_change_the_approved_image_after_visual_review(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        analyze = adapters.analyze_approved

        def mutate_approved(approved_image: Path) -> dict:
            approved_image.write_bytes(approved_image.read_bytes() + b"changed-during-analysis")
            return analyze(approved_image)

        adapters.analyze_approved = mutate_approved
        result = run_production(
            {**self.request, "productionItemId": "approved-image-mutated-by-analysis"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "template-analysis.json").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_object_adapter_seams_reject_non_object_results_stably(self) -> None:
        mutations = {
            "source": lambda adapters: setattr(adapters, "analyze_source", lambda *_args: []),
            "approved": lambda adapters: setattr(adapters, "analyze_approved", lambda *_args: []),
            "semantic-audit": lambda adapters: setattr(adapters, "audit_semantics", lambda *_args: []),
            "upload": lambda adapters: setattr(adapters, "upload", lambda *_args: []),
        }

        for name, mutate in mutations.items():
            with self.subTest(adapter=name):
                adapters = DeterministicFixtureAdapters(FIXTURE)
                mutate(adapters)
                result = run_production(
                    {**self.request, "productionItemId": f"non-object-{name}-adapter"},
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
                self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_source_read_adapters_receive_read_only_snapshots(self) -> None:
        original_source = Path(self.request["sourceImage"])
        original_bytes = original_source.read_bytes()

        for operation in ("analyze_source", "generate"):
            with self.subTest(adapter=operation):
                adapters = DeterministicFixtureAdapters(FIXTURE)
                original_method = getattr(adapters, operation)

                def mutate_source(source_image: Path, payload: dict, method=original_method) -> dict:
                    source_image.write_bytes(source_image.read_bytes() + b"adapter-mutation")
                    return method(source_image, payload)

                setattr(adapters, operation, mutate_source)
                result = run_production(
                    {
                        **self.request,
                        "productionItemId": f"readonly-source-{operation.replace('_', '-')}",
                    },
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
                self.assertEqual(original_bytes, original_source.read_bytes())
                self.assertEqual([], adapters.upload_calls)

    def test_upload_cannot_change_the_approved_image_after_validation(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)
        upload = adapters.upload

        def mutate_approved(approved_image: Path, object_key: str) -> dict:
            approved_image.write_bytes(approved_image.read_bytes() + b"upload-mutation")
            return upload(approved_image, object_key)

        adapters.upload = mutate_approved
        result = run_production(
            {**self.request, "productionItemId": "readonly-approved-upload"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        self.assertFalse((result.output_dir / "gallery-template.json").exists())
        self.assertEqual([], adapters.upload_calls)

    def test_image_adapters_cannot_delete_core_artifacts_through_snapshot_paths(self) -> None:
        operations = ("inspect_generated", "analyze_approved", "upload")
        for operation in operations:
            with self.subTest(adapter=operation):
                adapters = DeterministicFixtureAdapters(FIXTURE)

                def delete_snapshot(image_path: Path, *_args) -> dict:
                    image_path.unlink()
                    return {}

                setattr(adapters, operation, delete_snapshot)
                result = run_production(
                    {
                        **self.request,
                        "productionItemId": f"delete-snapshot-{operation.replace('_', '-')}",
                    },
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
                self.assertFalse((result.output_dir / "gallery-template.json").exists())
                candidate = result.output_dir / "evidence" / "generated-candidate-image.ppm"
                if candidate.exists():
                    self.assertTrue(candidate.is_file())
                approved = result.output_dir / "evidence" / "approved-template-image.ppm"
                if approved.exists():
                    self.assertTrue(approved.is_file())

    def test_image_adapter_snapshot_cannot_be_replaced_with_a_symlink(self) -> None:
        adapters = DeterministicFixtureAdapters(FIXTURE)

        def replace_snapshot(image_path: Path) -> dict:
            image_path.unlink()
            image_path.symlink_to(FIXTURE / "source-image.ppm")
            return {}

        adapters.analyze_approved = replace_snapshot
        result = run_production(
            {**self.request, "productionItemId": "symlinked-approved-snapshot"},
            self.output_root,
            adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["failed"], result.state)
        self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
        approved = result.output_dir / "evidence" / "approved-template-image.ppm"
        self.assertTrue(approved.is_file())
        self.assertFalse(approved.is_symlink())
        self.assertEqual([], adapters.upload_calls)

    def test_image_adapter_cannot_delete_the_core_path_captured_outside_its_snapshot(self) -> None:
        for operation in (
            "analyze_source",
            "generate",
            "inspect_generated",
            "analyze_approved",
            "upload",
        ):
            with self.subTest(adapter=operation):
                item_id = f"delete-core-{operation.replace('_', '-')}"
                caller_source = self.output_root / f"{item_id}.ppm"
                caller_source.write_bytes(Path(self.request["sourceImage"]).read_bytes())
                request = {**self.request, "productionItemId": item_id, "sourceImage": str(caller_source)}
                output_dir = self.output_root / item_id
                if operation in {"analyze_source", "generate"}:
                    core_path = caller_source
                elif operation == "inspect_generated":
                    core_path = output_dir / "evidence" / "generated-candidate-image.ppm"
                else:
                    core_path = output_dir / "evidence" / "approved-template-image.ppm"
                adapters = DeterministicFixtureAdapters(FIXTURE)
                original_method = getattr(adapters, operation)

                def delete_core_path(snapshot: Path, *args, method=original_method) -> dict:
                    core_path.unlink()
                    return method(snapshot, *args)

                setattr(adapters, operation, delete_core_path)
                result = run_production(
                    request,
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["failed"], result.state)
                self.assertEqual(RULES["errorCodes"]["externalFailure"], result.error_code)
                self.assertFalse((result.output_dir / "gallery-template.json").exists())

    def test_changed_approved_image_invalidates_every_dependent_visual_fact(self) -> None:
        request = {**self.request, "productionItemId": "changed-approved-image"}
        first = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        approved = first.output_dir / "evidence" / "approved-template-image.ppm"
        approved.write_bytes(approved.read_bytes() + b"changed")
        adapters = DeterministicFixtureAdapters(FIXTURE)

        invalidated = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RULES["resultStates"]["blocked"], invalidated.state)
        self.assertEqual(RULES["errorCodes"]["productionItemIntegrityFailure"], invalidated.error_code)
        manifest = load_json(first.output_dir / "production-manifest.json")
        event = manifest["invalidationEvents"][-1]
        self.assertEqual(RULES["invalidationReasons"]["approvedImageChanged"], event["reason"])
        self.assertEqual(
            [item["phase"] for item in RULES["productionPhases"][3:]],
            event["invalidatedPhases"],
        )
        self.assertTrue(
            {
                "template-analysis.json",
                "editable-template-spec.json",
                "gallery-template.draft.json",
                "validation-report.json",
                "asset-receipt.json",
                "gallery-template.json",
            }
            <= set(event["invalidatedArtifacts"])
        )
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_changed_generation_package_invalidates_p2_and_all_downstream_artifacts(self) -> None:
        request = {**self.request, "productionItemId": "changed-generation-package"}
        first = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        package_path = first.output_dir / "generation-package.json"
        package = load_json(package_path)
        package["sections"]["replacementTarget"] = "tampered generation fact"
        package_path.write_text(
            json.dumps(package, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        invalidated = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RULES["resultStates"]["blocked"], invalidated.state)
        self.assertEqual(RULES["errorCodes"]["productionItemIntegrityFailure"], invalidated.error_code)
        manifest = load_json(first.output_dir / "production-manifest.json")
        event = manifest["invalidationEvents"][-1]
        self.assertEqual(RULES["invalidationReasons"]["generationFactsChanged"], event["reason"])
        self.assertEqual(
            [item["phase"] for item in RULES["productionPhases"][2:]],
            event["invalidatedPhases"],
        )
        self.assertTrue(
            {
                "visual-review.json",
                "evidence/approved-template-image.ppm",
                "template-analysis.json",
                "asset-receipt.json",
                "gallery-template.json",
            }
            <= set(event["invalidatedArtifacts"])
        )
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_manifest_current_revision_must_match_immutable_artifact_lineage(self) -> None:
        request = {**self.request, "productionItemId": "invalid-current-revision"}
        first = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        manifest_path = first.output_dir / "production-manifest.json"
        manifest = load_json(manifest_path)
        manifest["revision"] = 2
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        result = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RULES["resultStates"]["blocked"], result.state)
        self.assertEqual(RULES["errorCodes"]["productionItemIntegrityFailure"], result.error_code)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)

    def test_completed_item_requires_the_complete_current_p2_artifact_quartet(self) -> None:
        missing_artifacts = (
            "generation-package.json",
            "evidence/generated-candidate-image.ppm",
            "visual-review.json",
            "evidence/approved-template-image.ppm",
        )
        for index, missing_name in enumerate(missing_artifacts):
            with self.subTest(missing_name=missing_name):
                request = {
                    **self.request,
                    "productionItemId": f"missing-current-p2-artifact-{index}",
                }
                completed = run_production(
                    request,
                    self.output_root,
                    DeterministicFixtureAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                )
                manifest_path = completed.output_dir / "production-manifest.json"
                manifest = load_json(manifest_path)
                manifest["artifacts"].pop(missing_name)
                (completed.output_dir / missing_name).unlink()
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                adapters = DeterministicFixtureAdapters(FIXTURE)

                result = run_production(
                    request,
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(
                    RULES["errorCodes"]["productionItemIntegrityFailure"],
                    result.error_code,
                )
                self.assertEqual([], adapters.generate_calls)
                self.assertEqual([], adapters.upload_calls)

    def test_non_object_current_image_artifact_records_are_stable_integrity_failures(self) -> None:
        for index, malformed_name in enumerate(
            (
                "evidence/generated-candidate-image.ppm",
                "evidence/approved-template-image.ppm",
            )
        ):
            with self.subTest(malformed_name=malformed_name):
                request = {
                    **self.request,
                    "productionItemId": f"malformed-current-image-record-{index}",
                }
                completed = run_production(
                    request,
                    self.output_root,
                    DeterministicFixtureAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                )
                manifest_path = completed.output_dir / "production-manifest.json"
                manifest = load_json(manifest_path)
                manifest["artifacts"][malformed_name] = []
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                adapters = DeterministicFixtureAdapters(FIXTURE)

                result = run_production(
                    request,
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(
                    RULES["errorCodes"]["productionItemIntegrityFailure"],
                    result.error_code,
                )
                self.assertEqual([], adapters.generate_calls)
                self.assertEqual([], adapters.upload_calls)

    def test_invalid_current_artifact_paths_are_stable_integrity_failures(self) -> None:
        for index, malformed_name in enumerate(
            (
                "generation-package.json",
                "evidence/generated-candidate-image.ppm",
            )
        ):
            with self.subTest(malformed_name=malformed_name):
                request = {
                    **self.request,
                    "productionItemId": f"malformed-current-artifact-path-{index}",
                }
                completed = run_production(
                    request,
                    self.output_root,
                    DeterministicFixtureAdapters(FIXTURE),
                    clock=lambda: FIXED_TIME,
                )
                manifest_path = completed.output_dir / "production-manifest.json"
                manifest = load_json(manifest_path)
                manifest["artifacts"][malformed_name]["path"] = 42
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                adapters = DeterministicFixtureAdapters(FIXTURE)

                result = run_production(
                    request,
                    self.output_root,
                    adapters,
                    clock=lambda: FIXED_TIME,
                )

                self.assertEqual(RULES["resultStates"]["blocked"], result.state)
                self.assertEqual(
                    RULES["errorCodes"]["productionItemIntegrityFailure"],
                    result.error_code,
                )
                self.assertEqual([], adapters.generate_calls)
                self.assertEqual([], adapters.upload_calls)

    def test_visual_hard_failure_redoes_p2_without_repeating_valid_p0_p1_work(self) -> None:
        contract = RULES["visualReviewContract"]
        dependency_gate = contract["hardGateRoles"]["dependencyClosure"]
        item_request = {**self.request, "productionItemId": "recover-visual-hard-failure"}
        first_adapters = DeterministicFixtureAdapters(FIXTURE).with_visual_review(
            {"hardGates": {dependency_gate: False}}
        )

        first = run_production(
            item_request,
            self.output_root,
            first_adapters,
            clock=lambda: FIXED_TIME,
        )
        second_adapters = DeterministicFixtureAdapters(FIXTURE)

        def p0_must_not_repeat(*_args):
            raise AssertionError("P0 source analysis repeated during P2 recovery")

        second_adapters.analyze_source = p0_must_not_repeat
        recovered = run_production(
            item_request,
            self.output_root,
            second_adapters,
            clock=lambda: FIXED_TIME,
        )

        self.assertEqual(RULES["resultStates"]["blocked"], first.state)
        self.assertEqual(RULES["resultStates"]["completed"], recovered.state)
        self.assertTrue(recovered.resumed)
        self.assertEqual(1, len(second_adapters.generate_calls))
        manifest = load_json(recovered.output_dir / "production-manifest.json")
        self.assertEqual(2, manifest["revision"])
        self.assertEqual(
            [item["phase"] for item in RULES["productionPhases"]],
            [item["phase"] for item in manifest["history"]],
        )
        package_v1 = load_json(recovered.output_dir / "generation-package.json")
        package_v2 = load_json(recovered.output_dir / "generation-package-r2.json")
        review_v1 = load_json(recovered.output_dir / "visual-review.json")
        review_v2 = load_json(recovered.output_dir / "visual-review-r2.json")
        approved_v2 = recovered.output_dir / "evidence" / "approved-template-image-r2.ppm"
        analysis = load_json(recovered.output_dir / "template-analysis.json")
        self.assertNotEqual(package_v1["requestId"], package_v2["requestId"])
        self.assertEqual(contract["decisionValues"]["rejected"], review_v1["decision"])
        self.assertEqual(contract["decisionValues"]["approved"], review_v2["decision"])
        self.assertEqual(canonical_sha(package_v2), review_v2["bindings"]["generationPackageSha256"])
        self.assertEqual(hashlib.sha256(approved_v2.read_bytes()).hexdigest(), analysis["visualFactSourceSha256"])
        self.assertEqual(1, manifest["artifacts"]["generation-package.json"]["revision"])
        self.assertEqual(2, manifest["artifacts"]["generation-package-r2.json"]["revision"])
        invalidation = manifest["invalidationEvents"][0]
        self.assertEqual(RULES["invalidationReasons"]["generationFactsChanged"], invalidation["reason"])
        self.assertEqual("generation-package.json", invalidation["supersededArtifact"])
        self.assertEqual("generation-package-r2.json", invalidation["replacementArtifact"])
        self.assertEqual(
            {
                "evidence/generated-candidate-image.ppm",
                "visual-review.json",
            },
            set(invalidation["invalidatedArtifacts"]),
        )
        self.assertEqual(
            [item["phase"] for item in RULES["productionPhases"][2:]],
            invalidation["invalidatedPhases"],
        )
        self.assertEqual(1, len(second_adapters.upload_calls))

    def test_revision_two_p7_recovery_reuses_the_current_approved_image_and_receipt(self) -> None:
        dependency_gate = RULES["visualReviewContract"]["hardGateRoles"]["dependencyClosure"]
        request = {**self.request, "productionItemId": "recover-revision-two-p7"}
        run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE).with_visual_review(
                {"hardGates": {dependency_gate: False}}
            ),
            clock=lambda: FIXED_TIME,
        )
        completed = run_production(
            request,
            self.output_root,
            DeterministicFixtureAdapters(FIXTURE),
            clock=lambda: FIXED_TIME,
        )
        manifest = load_json(completed.output_dir / "production-manifest.json")
        uploaded_phase = RULES["productionPhases"][7]
        manifest["phase"] = uploaded_phase["phase"]
        manifest["state"] = uploaded_phase["state"]
        manifest["outcome"] = None
        manifest["history"] = [
            item for item in manifest["history"] if item["phase"] != RULES["productionPhases"][8]["phase"]
        ]
        for name in ("final-validation-report.json", "gallery-template.json"):
            manifest["artifacts"].pop(name)
            (completed.output_dir / name).unlink()
        (completed.output_dir / "production-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        adapters = DeterministicFixtureAdapters(FIXTURE)

        recovered = run_production(request, self.output_root, adapters, clock=lambda: FIXED_TIME)

        self.assertEqual(RULES["resultStates"]["completed"], recovered.state)
        self.assertTrue(recovered.resumed)
        self.assertEqual([], adapters.generate_calls)
        self.assertEqual([], adapters.upload_calls)


if __name__ == "__main__":
    unittest.main()
