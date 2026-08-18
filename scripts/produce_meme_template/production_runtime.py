from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json as _load_json,
    pretty_json_bytes as _json_bytes,
    sha256_bytes as _sha_bytes,
    sha256_file as _sha_file,
)
from .release_management import doctor
from .template_compiler import (
    _compile_draft,
    _compile_editable_spec,
    _compile_hidden_spec,
    _formal_projection,
    _semantic_audit_payload,
    _validate_final,
    _validation_report,
)
from .workflow import (
    GALLERY_SCHEMA_PATH,
    RELEASE_PATH,
    REPO_ROOT,
    RULES_PATH,
    ProductionResult,
    WorkflowAdapters,
    WorkflowStop,
    _adapter_object_call,
    _adapter_snapshot_image_object_call,
    _adopt_pre_submit_generation_staging,
    _advance,
    _append_invalidation_event,
    _artifact_descendants,
    _asset_receipt_valid,
    _atomic_write_new,
    _build_asset_receipt,
    _build_pin,
    _changed_lineage_artifacts,
    _compile_generation_package,
    _compile_generation_task,
    _compile_redo_generation_package,
    _current_finalization_errors,
    _current_generation_execution_errors,
    _current_item_fact_errors,
    _current_p2_artifact_errors,
    _current_shared_policy_resolution_errors,
    _delivery_image_context,
    _evaluate_visual_gate,
    _file_matches_sha,
    _finalize_uploaded_item,
    _generation_failure_stop,
    _generation_poll_shape_valid,
    _generation_submission_shape_valid,
    _isolated_output_dir,
    _load_generation_execution_evidence,
    _normalize_replacement_strategy,
    _normalized_generation_options,
    _object_storage_key,
    _persist_manifest,
    _plan_replacement,
    _prepared_generation_wal,
    _production_item_integrity_errors,
    _production_request_errors,
    _record_artifact,
    _revision_image_artifacts,
    _revisioned_name,
    _sanitize_generation_failure_reason,
    _source_analysis_identity_valid,
    _stop,
    _upload_result_valid,
    _write_generation_wal,
)


def _run_single_production(
    request: dict[str, Any],
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None = None,
    prepared_source_analysis: dict[str, Any] | None = None,
    shared_policy_resolution: dict[str, Any] | None = None,
    preparation_stop: WorkflowStop | None = None,
) -> ProductionResult:
    """Run one independent Production Item through P0-P8."""

    rules = _load_json(RULES_PATH)
    release = _load_json(RELEASE_PATH)
    p0, p1, p2, p3, p4, p5, p6, p7, p8 = (item["phase"] for item in rules["productionPhases"])
    now = clock or (lambda: datetime.now(timezone.utc))
    timestamp = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    output_root_path = Path(output_root).resolve()
    schema = _load_json(GALLERY_SCHEMA_PATH)
    template_key = str(request.get("templateKey", ""))
    production_item_id = request.get("productionItemId")
    request_errors = _production_request_errors(request, rules, schema)
    if request_errors:
        return ProductionResult(
            "needs_input",
            str(production_item_id or "invalid-production-item"),
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="生产请求预检失败：" + "；".join(request_errors),
        )
    replacement_strategy = _normalize_replacement_strategy(request, rules)
    generation_options = _normalized_generation_options(request, rules)
    source_image = Path(request["sourceImage"]).resolve()
    if not source_image.is_file():
        raise FileNotFoundError(source_image)
    source_sha = _sha_file(source_image)
    replacement_strategy_identity: Any = replacement_strategy
    if shared_policy_resolution is not None:
        replacement_strategy_identity = {
            "replacementStrategy": replacement_strategy,
            "sharedPolicyResolution": shared_policy_resolution,
        }
    replacement_strategy_sha = _sha_bytes(
        _canonical_bytes(replacement_strategy_identity)
    )
    generation_options_sha = _sha_bytes(_canonical_bytes(generation_options))
    item_id = str(production_item_id or f"{template_key}-{source_sha[:12]}")
    output_dir = _isolated_output_dir(output_root_path, item_id)
    if output_dir is None:
        return ProductionResult(
            "needs_input",
            item_id,
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="Production Item 输出目录越出 output root。",
        )
    manifest_path = output_dir / "production-manifest.json"
    existing_pin: dict[str, Any] | None = None
    pin_path = output_dir / "production-pin.json"
    if pin_path.is_file():
        try:
            raw_pin = _load_json(pin_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raw_pin = None
        if isinstance(raw_pin, dict):
            existing_pin = raw_pin
    runtime_diagnosis = doctor(REPO_ROOT, production_pin=existing_pin)
    diagnostic_fields = rules["releaseManagementContract"][
        "diagnosticFields"
    ]
    if not runtime_diagnosis["pass"]:
        return ProductionResult(
            "blocked",
            item_id,
            rules["resultStates"]["blocked"],
            output_dir,
            error_code=rules["errorCodes"]["versionDiagnosticFailure"],
            message="运行前 doctor 检查未通过："
            + "、".join(
                runtime_diagnosis[diagnostic_fields["errorCodes"]]
            ),
            resumed=manifest_path.is_file(),
        )
    resume_visual = False
    resume_generation = False
    resume_prepared_generation = False
    reuse_succeeded_generation = False
    resumed = False
    source_analysis: dict[str, Any]
    plan: dict[str, Any]
    generation_package: dict[str, Any]
    generation_task: dict[str, Any]
    generation_wal: dict[str, Any]
    generation_submission: dict[str, Any]
    if manifest_path.exists():
        try:
            existing = _load_json(manifest_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            existing = None
        if not isinstance(existing, dict):
            return ProductionResult(
                "blocked",
                item_id,
                rules["resultStates"]["blocked"],
                output_dir,
                error_code=rules["errorCodes"][
                    "productionItemIntegrityFailure"
                ],
                message="Production Manifest 无法读取或顶层形状无效。",
                resumed=True,
            )
        completed_artifacts = ("production-pin.json", "gallery-template.json", "final-validation-report.json")
        identity_errors = _production_item_integrity_errors(
            output_dir,
            existing,
            production_item_id=item_id,
            template_key=template_key,
            source_sha256=source_sha,
            replacement_strategy_sha256=replacement_strategy_sha,
            generation_options_sha256=generation_options_sha,
            required_artifacts=completed_artifacts if existing.get("state") == rules["resultStates"]["completed"] else (),
        )
        if shared_policy_resolution is not None:
            identity_errors.extend(
                _current_shared_policy_resolution_errors(
                    output_dir,
                    shared_policy_resolution,
                    rules,
                )
            )
        if existing.get("state") == rules["resultStates"]["completed"]:
            identity_errors.extend(_current_p2_artifact_errors(existing))
            identity_errors.extend(
                _current_generation_execution_errors(
                    output_dir,
                    existing,
                    source_sha,
                    generation_options,
                    rules,
                )
            )
            identity_errors.extend(
                _current_finalization_errors(output_dir, existing, rules)
            )
            identity_errors.extend(
                _current_item_fact_errors(output_dir, existing, rules)
            )
        existing_revision_for_wal = existing.get("revision")
        if (
            existing.get("state") != rules["resultStates"]["completed"]
            and isinstance(existing_revision_for_wal, int)
            and not isinstance(existing_revision_for_wal, bool)
            and existing.get("phase") != rules["productionPhases"][7]["phase"]
        ):
            current_wal_name = _revisioned_name(
                "generation-wal.json", existing_revision_for_wal
            )
            deferred_wal_digest_error = f"{current_wal_name} digest mismatch"
            if deferred_wal_digest_error in identity_errors:
                identity_errors.remove(deferred_wal_digest_error)
        if existing.get("state") == rules["resultStates"]["completed"]:
            revision = existing.get("revision")
            generation_fact_names = [
                _revisioned_name("generation-package.json", revision),
                *_revision_image_artifacts(existing, "generated-candidate-image", revision),
            ]
            changed_generation_facts = _changed_lineage_artifacts(
                output_dir,
                existing,
                generation_fact_names,
            )
            if changed_generation_facts:
                changed_name = changed_generation_facts[0]
                changed_path = output_dir / changed_name
                _append_invalidation_event(
                    existing,
                    rules,
                    reason_key="generationFactsChanged",
                    superseded_artifact=changed_name,
                    observed_sha256=_sha_file(changed_path) if changed_path.is_file() else None,
                    invalidated_artifacts=_artifact_descendants(existing, changed_name),
                    invalidated_from_phase=p2,
                    timestamp=timestamp,
                )
                existing["phase"] = p1
                existing["state"] = rules["resultStates"]["blocked"]
                existing["outcome"] = "blocked"
                existing["error"] = {
                    "code": rules["errorCodes"]["productionItemIntegrityFailure"],
                    "message": "上游生图事实摘要发生变化，P2 及下游产物已失效。",
                    "evidence": {"artifact": changed_name},
                }
                _persist_manifest(output_dir, existing)
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message=existing["error"]["message"],
                    resumed=True,
                )
            approved_names = _revision_image_artifacts(
                existing,
                "approved-template-image",
                revision,
            )
            changed_approved = _changed_lineage_artifacts(output_dir, existing, approved_names)
            if len(changed_approved) == 1:
                changed_name = changed_approved[0]
                changed_path = output_dir / changed_name
                _append_invalidation_event(
                    existing,
                    rules,
                    reason_key="approvedImageChanged",
                    superseded_artifact=changed_name,
                    observed_sha256=_sha_file(changed_path) if changed_path.is_file() else None,
                    invalidated_artifacts=_artifact_descendants(existing, changed_name),
                    invalidated_from_phase=p3,
                    timestamp=timestamp,
                )
                existing["phase"] = p2
                existing["state"] = rules["resultStates"]["blocked"]
                existing["outcome"] = "blocked"
                existing["error"] = {
                    "code": rules["errorCodes"]["productionItemIntegrityFailure"],
                    "message": "确认模板图摘要发生变化，依赖视觉事实已失效。",
                    "evidence": {"artifact": changed_name},
                }
                _persist_manifest(output_dir, existing)
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message=existing["error"]["message"],
                    resumed=True,
                )
        if existing.get("state") == rules["resultStates"]["completed"] and identity_errors:
            return ProductionResult(
                "blocked",
                item_id,
                rules["resultStates"]["blocked"],
                output_dir,
                error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                message="已完成 Production Item 的身份或产物谱系校验失败：" + "；".join(identity_errors),
            )
        if existing.get("state") == rules["resultStates"]["completed"]:
            return ProductionResult(
                "completed",
                item_id,
                rules["resultStates"]["completed"],
                output_dir,
                output_dir / "gallery-template.json",
                resumed=True,
            )
        if identity_errors and any(error.endswith("mismatch") for error in identity_errors):
            return ProductionResult(
                "blocked",
                item_id,
                rules["resultStates"]["blocked"],
                output_dir,
                error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                message="Production Item 请求身份与已有状态不一致：" + "；".join(identity_errors),
            )
        uploaded_phase = rules["productionPhases"][7]
        if existing.get("phase") == uploaded_phase["phase"] and existing.get("state") == uploaded_phase["state"]:
            recovery_errors = _production_item_integrity_errors(
                output_dir,
                existing,
                production_item_id=item_id,
                template_key=template_key,
                source_sha256=source_sha,
                replacement_strategy_sha256=replacement_strategy_sha,
                generation_options_sha256=generation_options_sha,
                required_artifacts=(
                    "production-pin.json",
                    "gallery-template.draft.json",
                    "validation-report.json",
                    "asset-receipt.json",
                ),
            )
            recovery_errors.extend(_current_p2_artifact_errors(existing))
            recovery_errors.extend(
                _current_generation_execution_errors(
                    output_dir,
                    existing,
                    source_sha,
                    generation_options,
                    rules,
                )
            )
            recovery_errors.extend(
                _current_item_fact_errors(output_dir, existing, rules)
            )
            if recovery_errors:
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message="P7 Production Item 的身份或产物谱系校验失败：" + "；".join(recovery_errors),
                )
            try:
                return _finalize_uploaded_item(output_dir, existing, rules, timestamp)
            except WorkflowStop as stop:
                existing["state"] = stop.state
                existing["outcome"] = stop.outcome
                existing["error"] = {
                    "code": stop.error_code,
                    "message": stop.message,
                    "evidence": stop.evidence,
                }
                _persist_manifest(output_dir, existing)
                return ProductionResult(
                    stop.outcome,
                    item_id,
                    stop.state,
                    output_dir,
                    error_code=stop.error_code,
                    message=stop.message,
                    resumed=True,
                )
        existing_error_code = existing.get("error", {}).get("code")
        existing_revision = existing.get("revision")
        if isinstance(existing_revision, int) and existing_error_code != rules["errorCodes"][
            "visualHardFailure"
        ]:
            generation_package_name = _revisioned_name(
                "generation-package.json", existing_revision
            )
            generation_task_name = _revisioned_name("generation-task.json", existing_revision)
            generation_wal_name = _revisioned_name("generation-wal.json", existing_revision)
            generation_task_path = output_dir / generation_task_name
            generation_wal_path = output_dir / generation_wal_name
            if generation_task_path.is_file() or generation_wal_path.is_file():
                staging_names = (
                    generation_package_name,
                    generation_task_name,
                    generation_wal_name,
                )
                if any(name not in existing["artifacts"] for name in staging_names):
                    staging_errors = _production_item_integrity_errors(
                        output_dir,
                        existing,
                        production_item_id=item_id,
                        template_key=template_key,
                        source_sha256=source_sha,
                        replacement_strategy_sha256=replacement_strategy_sha,
                        generation_options_sha256=generation_options_sha,
                        required_artifacts=(
                            "production-pin.json",
                            "source-analysis.json",
                            "replacement-plan.json",
                        ),
                    )
                    if not staging_errors:
                        (
                            staging_errors,
                            generation_package,
                            generation_task,
                            generation_wal,
                        ) = _adopt_pre_submit_generation_staging(
                            output_dir,
                            existing,
                            source_sha,
                            generation_options,
                            rules,
                            timestamp,
                            p2,
                        )
                    if staging_errors:
                        return ProductionResult(
                            "blocked",
                            item_id,
                            rules["resultStates"]["blocked"],
                            output_dir,
                            error_code=rules["errorCodes"][
                                "productionItemIntegrityFailure"
                            ],
                            message="P2 提交前 staging 恢复校验失败："
                            + "；".join(staging_errors),
                            resumed=True,
                        )
                recovery_errors = _production_item_integrity_errors(
                    output_dir,
                    existing,
                    production_item_id=item_id,
                    template_key=template_key,
                    source_sha256=source_sha,
                    replacement_strategy_sha256=replacement_strategy_sha,
                    generation_options_sha256=generation_options_sha,
                    required_artifacts=(
                        "production-pin.json",
                        "source-analysis.json",
                        "replacement-plan.json",
                        generation_package_name,
                        generation_task_name,
                        generation_wal_name,
                    ),
                )
                wal_digest_error = f"{generation_wal_name} digest mismatch"
                wal_manifest_lag = wal_digest_error in recovery_errors
                if wal_manifest_lag:
                    recovery_errors.remove(wal_digest_error)
                if not generation_task_path.is_file() or not generation_wal_path.is_file():
                    recovery_errors.append("generation task or WAL file missing")
                if not recovery_errors:
                    (
                        execution_errors,
                        generation_package,
                        generation_task,
                        generation_wal,
                    ) = _load_generation_execution_evidence(
                            output_dir,
                            generation_package_name,
                            generation_task_name,
                            generation_wal_name,
                            source_sha,
                            existing_revision,
                            generation_options,
                            rules,
                        )
                    recovery_errors.extend(execution_errors)
                    if wal_manifest_lag and not execution_errors:
                        recorded_wal = existing["artifacts"].get(generation_wal_name)
                        previous_wal_sha = generation_wal[
                            rules["generationExecutionContract"]["walFields"][
                                "previousWalSha256"
                            ]
                        ]
                        if (
                            not isinstance(recorded_wal, dict)
                            or previous_wal_sha != recorded_wal.get("sha256")
                        ):
                            recovery_errors.append(
                                "generation WAL does not continue the recorded digest"
                            )
                        else:
                            _record_artifact(
                                existing,
                                output_dir,
                                generation_wal_name,
                                p2,
                                [generation_task_name],
                            )
                            _persist_manifest(output_dir, existing)
                if recovery_errors:
                    return ProductionResult(
                        "blocked",
                        item_id,
                        rules["resultStates"]["blocked"],
                        output_dir,
                        error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                        message="生成任务恢复前的身份、WAL 或谱系校验失败："
                        + "；".join(recovery_errors),
                        resumed=True,
                    )
                execution_contract = rules["generationExecutionContract"]
                wal_fields = execution_contract["walFields"]
                failure_class = generation_wal[wal_fields["failureClass"]]
                provider_request_id = generation_wal[
                    wal_fields["providerRequestIdentity"]
                ]
                wal_status = generation_wal[wal_fields["status"]]
                can_resume_poll = (
                    wal_status == execution_contract["walStatuses"]["submitted"]
                    or failure_class
                    == execution_contract["failureClasses"]["retryable"]
                )
                can_resume_prepared = (
                    wal_status == execution_contract["walStatuses"]["prepared"]
                )
                can_reuse_candidate = (
                    wal_status == execution_contract["walStatuses"]["succeeded"]
                )
                if can_resume_prepared:
                    manifest = existing
                    source_analysis = _load_json(output_dir / "source-analysis.json")
                    plan = _load_json(output_dir / "replacement-plan.json")
                    manifest["state"] = next(
                        item["state"]
                        for item in rules["productionPhases"]
                        if item["phase"] == p1
                    )
                    manifest["outcome"] = None
                    manifest.pop("error", None)
                    _persist_manifest(output_dir, manifest)
                    resume_prepared_generation = True
                    resumed = True
                elif (
                    (can_resume_poll or can_reuse_candidate)
                    and isinstance(provider_request_id, str)
                    and provider_request_id.strip()
                ):
                    if can_reuse_candidate:
                        task_fields = execution_contract["taskFields"]
                        intent_fields = execution_contract["requestIntentFields"]
                        output_format = generation_task[task_fields["requestIntent"]][
                            intent_fields["outputFormat"]
                        ]
                        output_format_role = next(
                            role
                            for role, value in execution_contract[
                                "outputFormats"
                            ].items()
                            if value == output_format
                        )
                        expected_candidate_name = _revisioned_name(
                            "evidence/generated-candidate-image"
                            + execution_contract["outputFormatExtensions"][
                                output_format_role
                            ],
                            existing_revision,
                        )
                        expected_candidate_path = output_dir / expected_candidate_name
                        if (
                            expected_candidate_name not in existing["artifacts"]
                            and _file_matches_sha(
                                expected_candidate_path,
                                generation_wal[wal_fields["outputSha256"]],
                            )
                        ):
                            _record_artifact(
                                existing,
                                output_dir,
                                expected_candidate_name,
                                p2,
                                [
                                    generation_package_name,
                                    generation_task_name,
                                    generation_wal_name,
                                ],
                            )
                            _persist_manifest(output_dir, existing)
                        recovery_errors.extend(
                            _current_generation_execution_errors(
                                output_dir,
                                existing,
                                source_sha,
                                generation_options,
                                rules,
                            )
                        )
                        if recovery_errors:
                            return ProductionResult(
                                "blocked",
                                item_id,
                                rules["resultStates"]["blocked"],
                                output_dir,
                                error_code=rules["errorCodes"][
                                    "productionItemIntegrityFailure"
                                ],
                                message="成功生成任务的本地候选图或 WAL 对账失败："
                                + "；".join(recovery_errors),
                                resumed=True,
                            )
                    submission_fields = execution_contract["submissionFields"]
                    generation_submission = {
                        submission_fields["status"]: execution_contract[
                            "submissionStatuses"
                        ]["submitted"],
                        submission_fields["provider"]: generation_wal[
                            wal_fields["provider"]
                        ],
                        submission_fields["model"]: generation_wal[wal_fields["model"]],
                        submission_fields["providerRequestIdentity"]: provider_request_id,
                        submission_fields["failureClass"]: None,
                        submission_fields["failureReason"]: None,
                    }
                    manifest = existing
                    source_analysis = _load_json(output_dir / "source-analysis.json")
                    plan = _load_json(output_dir / "replacement-plan.json")
                    manifest["state"] = next(
                        item["state"]
                        for item in rules["productionPhases"]
                        if item["phase"] == p1
                    )
                    manifest["outcome"] = None
                    manifest.pop("error", None)
                    if can_resume_poll:
                        generation_wal[wal_fields["status"]] = execution_contract[
                            "walStatuses"
                        ]["submitted"]
                        generation_wal[wal_fields["failureClass"]] = None
                        generation_wal[wal_fields["failureReason"]] = None
                        generation_wal[wal_fields["updatedAt"]] = timestamp
                        _write_generation_wal(generation_wal_path, generation_wal, rules)
                        _record_artifact(
                            manifest,
                            output_dir,
                            generation_wal_name,
                            p2,
                            [generation_task_name],
                        )
                    _persist_manifest(output_dir, manifest)
                    resume_generation = True
                    reuse_succeeded_generation = can_reuse_candidate
                    resumed = True
                else:
                    if failure_class not in execution_contract["failureClasses"].values():
                        failure_class = execution_contract["failureClasses"][
                            "submissionUnknown"
                        ]
                    stop = _generation_failure_stop(
                        failure_class,
                        generation_wal[wal_fields["failureReason"]]
                        or "provider submission state is uncertain",
                        rules,
                        {
                            "taskId": generation_wal[wal_fields["taskIdentity"]],
                            "providerRequestId": provider_request_id,
                        },
                    )
                    existing["state"] = stop.state
                    existing["outcome"] = stop.outcome
                    existing["error"] = {
                        "code": stop.error_code,
                        "message": stop.message,
                        "evidence": stop.evidence,
                    }
                    _persist_manifest(output_dir, existing)
                    return ProductionResult(
                        stop.outcome,
                        item_id,
                        stop.state,
                        output_dir,
                        error_code=stop.error_code,
                        message=stop.message,
                        resumed=True,
                    )
        if existing.get("error", {}).get("code") == rules["errorCodes"]["visualHardFailure"]:
            previous_revision = existing.get("revision")
            if not isinstance(previous_revision, int) or previous_revision < 1:
                recovery_errors = ["manifest revision invalid"]
            else:
                previous_package_name = _revisioned_name("generation-package.json", previous_revision)
                previous_task_name = _revisioned_name("generation-task.json", previous_revision)
                previous_wal_name = _revisioned_name("generation-wal.json", previous_revision)
                previous_review_name = _revisioned_name("visual-review.json", previous_revision)
                recovery_errors = _production_item_integrity_errors(
                    output_dir,
                    existing,
                    production_item_id=item_id,
                    template_key=template_key,
                    source_sha256=source_sha,
                    replacement_strategy_sha256=replacement_strategy_sha,
                    generation_options_sha256=generation_options_sha,
                    required_artifacts=(
                        "production-pin.json",
                        "source-analysis.json",
                        "replacement-plan.json",
                        previous_package_name,
                        previous_task_name,
                        previous_wal_name,
                        previous_review_name,
                    ),
                )
            if recovery_errors:
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message="P2 重做前的身份或产物谱系校验失败：" + "；".join(recovery_errors),
                    resumed=True,
                )
            manifest = existing
            source_analysis = _load_json(output_dir / "source-analysis.json")
            plan = _load_json(output_dir / "replacement-plan.json")
            previous_package = _load_json(output_dir / previous_package_name)
            previous_review = _load_json(output_dir / previous_review_name)
            manifest["revision"] = previous_revision + 1
            manifest["state"] = next(
                item["state"] for item in rules["productionPhases"] if item["phase"] == p1
            )
            manifest["outcome"] = None
            manifest.pop("error", None)
            generation_package = _compile_redo_generation_package(
                previous_package,
                previous_review,
                manifest["revision"],
            )
            replacement_package_name = _revisioned_name(
                "generation-package.json", manifest["revision"]
            )
            _append_invalidation_event(
                manifest,
                rules,
                reason_key="generationFactsChanged",
                superseded_artifact=previous_package_name,
                replacement_artifact=replacement_package_name,
                replacement_sha256=_sha_bytes(_json_bytes(generation_package)),
                invalidated_artifacts=_artifact_descendants(manifest, previous_package_name),
                invalidated_from_phase=p2,
                timestamp=timestamp,
            )
            resume_visual = True
            resumed = True
            _persist_manifest(output_dir, manifest)
    if not resume_visual and not resume_generation and not resume_prepared_generation:
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "artifactType": "production-manifest",
            "schemaVersion": rules["schemaVersion"],
            "productionItemId": item_id,
            "templateKey": template_key,
            "revision": 1,
            "sourceImageSha256": source_sha,
            "replacementStrategySha256": replacement_strategy_sha,
            "generationOptionsSha256": generation_options_sha,
            "phase": None,
            "state": rules["initialState"],
            "outcome": None,
            "history": [],
            "artifacts": {},
            "invalidationEvents": [],
            "historicalExperienceEvidence": rules[
                "historicalExperienceContract"
            ]["experienceIds"],
        }
    try:
        if not resume_visual and not resume_generation and not resume_prepared_generation:
            pin = _build_pin(rules, release)
            _atomic_write_new(output_dir / "production-pin.json", _json_bytes(pin))
            _record_artifact(manifest, output_dir, "production-pin.json", p0, [])
            evidence_source = output_dir / "evidence" / f"source-image{source_image.suffix.lower()}"
            _atomic_write_new(evidence_source, source_image.read_bytes())
            _record_artifact(manifest, output_dir, str(evidence_source.relative_to(output_dir)), p0, [])
            if preparation_stop is not None:
                raise preparation_stop
            source_analysis = (
                copy.deepcopy(prepared_source_analysis)
                if prepared_source_analysis is not None
                else _adapter_snapshot_image_object_call(
                    rules,
                    "analyze_source",
                    adapters.analyze_source,
                    source_image,
                    source_sha,
                    copy.deepcopy(replacement_strategy),
                )
            )
            if (
                source_analysis.get("sourceImageSha256") != source_sha
                or not _source_analysis_identity_valid(source_analysis, rules)
            ):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "来源分析证据与输入图片或主体身份不一致。",
                    {},
                )
            _atomic_write_new(output_dir / "source-analysis.json", _json_bytes(source_analysis))
            _record_artifact(manifest, output_dir, "source-analysis.json", p0, [str(evidence_source.relative_to(output_dir))])
            plan_dependencies = ["source-analysis.json"]
            if shared_policy_resolution is not None:
                resolution_name = rules["batchProductionContract"][
                    "resolutionArtifactName"
                ]
                _atomic_write_new(
                    output_dir / resolution_name,
                    _json_bytes(shared_policy_resolution),
                )
                _record_artifact(
                    manifest,
                    output_dir,
                    resolution_name,
                    p0,
                    ["source-analysis.json"],
                )
                plan_dependencies.append(resolution_name)
            _advance(manifest, rules, p0, timestamp)
            _persist_manifest(output_dir, manifest)

            plan = _plan_replacement(
                source_analysis,
                rules,
                template_key,
                replacement_strategy,
                shared_policy_resolution,
            )
            _atomic_write_new(output_dir / "replacement-plan.json", _json_bytes(plan))
            _record_artifact(
                manifest,
                output_dir,
                "replacement-plan.json",
                p1,
                plan_dependencies,
            )
            _advance(manifest, rules, p1, timestamp)
            _persist_manifest(output_dir, manifest)

            generation_package = _compile_generation_package(plan, source_analysis, rules)
        execution_contract = rules["generationExecutionContract"]
        task_fields = execution_contract["taskFields"]
        wal_fields = execution_contract["walFields"]
        submission_fields = execution_contract["submissionFields"]
        poll_fields = execution_contract["pollResultFields"]
        if not resume_generation:
            if not resume_prepared_generation:
                generation_package["output"]["imageCount"] = generation_options[
                    execution_contract["requestOptionFields"]["imageCount"]
                ]
                generation_package_name = _revisioned_name(
                    "generation-package.json", manifest["revision"]
                )
                _atomic_write_new(
                    output_dir / generation_package_name, _json_bytes(generation_package)
                )
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_package_name,
                    p2,
                    ["replacement-plan.json"],
                )
                generation_task_name = _revisioned_name(
                    "generation-task.json", manifest["revision"]
                )
                generation_wal_name = _revisioned_name(
                    "generation-wal.json", manifest["revision"]
                )
                generation_task_path = output_dir / generation_task_name
                generation_wal_path = output_dir / generation_wal_name
                generation_task = _compile_generation_task(
                    generation_package,
                    source_sha,
                    _sha_file(output_dir / "production-pin.json"),
                    manifest["revision"],
                    generation_options,
                    rules,
                )
                _atomic_write_new(generation_task_path, _json_bytes(generation_task))
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_task_name,
                    p2,
                    [generation_package_name, "production-pin.json"],
                )
                generation_wal = _prepared_generation_wal(
                    generation_task, timestamp, rules
                )
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
            package_request = copy.deepcopy(generation_package)
            task_request = copy.deepcopy(generation_task)
            submit_generation = getattr(adapters, "submit_generation", None)
            if not callable(submit_generation):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "生成 adapter 缺少 queued submit seam。",
                    {"operation": "submit_generation"},
                )
            try:
                generation_submission = _adapter_snapshot_image_object_call(
                    rules,
                    "submit_generation",
                    submit_generation,
                    source_image,
                    source_sha,
                    package_request,
                    task_request,
                )
            except WorkflowStop as adapter_stop:
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                generation_wal[wal_fields["failureClass"]] = execution_contract[
                    "failureClasses"
                ]["submissionUnknown"]
                generation_wal[wal_fields["failureReason"]] = adapter_stop.message
                generation_wal[wal_fields["updatedAt"]] = timestamp
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
                raise _generation_failure_stop(
                    execution_contract["failureClasses"]["submissionUnknown"],
                    adapter_stop.message,
                    rules,
                    {
                        "taskId": generation_task[task_fields["taskIdentity"]],
                        "adapterFailure": adapter_stop.evidence,
                    },
                ) from adapter_stop
            if (
                package_request != generation_package
                or task_request != generation_task
                or not _generation_submission_shape_valid(generation_submission, rules)
            ):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "生成提交结果未绑定冻结任务，或 adapter 修改了提交请求。",
                    {},
                )
            submission_status = generation_submission[submission_fields["status"]]
            if submission_status == execution_contract["submissionStatuses"]["failed"]:
                failure_class = generation_submission[
                    submission_fields["failureClass"]
                ]
                failure_reason = generation_submission[
                    submission_fields["failureReason"]
                ]
                failure_reason = _sanitize_generation_failure_reason(
                    failure_reason, rules
                )
                if failure_class == execution_contract["failureClasses"]["retryable"]:
                    failure_class = execution_contract["failureClasses"][
                        "submissionUnknown"
                    ]
                    failure_reason = "provider submission may have succeeded: " + failure_reason
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                generation_wal[wal_fields["provider"]] = generation_submission[
                    submission_fields["provider"]
                ]
                generation_wal[wal_fields["model"]] = generation_submission[
                    submission_fields["model"]
                ]
                generation_wal[wal_fields["failureClass"]] = failure_class
                generation_wal[wal_fields["failureReason"]] = failure_reason
                generation_wal[wal_fields["updatedAt"]] = timestamp
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
                raise _generation_failure_stop(
                    failure_class,
                    failure_reason,
                    rules,
                    {"taskId": generation_task[task_fields["taskIdentity"]]},
                )
            generation_wal[wal_fields["status"]] = execution_contract["walStatuses"][
                "submitted"
            ]
            for role in ("provider", "model", "providerRequestIdentity"):
                generation_wal[wal_fields[role]] = generation_submission[
                    submission_fields[role]
                ]
            generation_wal[wal_fields["failureClass"]] = None
            generation_wal[wal_fields["failureReason"]] = None
            generation_wal[wal_fields["updatedAt"]] = timestamp
            _write_generation_wal(generation_wal_path, generation_wal, rules)
            _record_artifact(
                manifest,
                output_dir,
                generation_wal_name,
                p2,
                [generation_task_name],
            )
            _persist_manifest(output_dir, manifest)
        package_request = copy.deepcopy(generation_package)
        task_request = copy.deepcopy(generation_task)
        submission_request = copy.deepcopy(generation_submission)
        if reuse_succeeded_generation:
            candidate_names = _revision_image_artifacts(
                manifest, "generated-candidate-image", manifest["revision"]
            )
            candidate_path = output_dir / candidate_names[0]
            poll_result = {
                poll_fields["status"]: execution_contract["pollStatuses"]["succeeded"],
                poll_fields["failureClass"]: None,
                poll_fields["failureReason"]: None,
                poll_fields["extension"]: candidate_path.suffix,
                poll_fields["imageBytes"]: candidate_path.read_bytes(),
                poll_fields["outputAssets"]: generation_wal[
                    wal_fields["outputAssets"]
                ],
                poll_fields["providerOutputIdentity"]: generation_wal[
                    wal_fields["providerOutputIdentity"]
                ],
            }
        else:
            retry_budget = execution_contract["retryBudgets"]["retryable"]
            if generation_wal[wal_fields["pollAttemptCount"]] >= retry_budget:
                failure_class = execution_contract["failureClasses"]["permanent"]
                failure_reason = _sanitize_generation_failure_reason(
                    "poll attempt budget exhausted before a safe retry", rules
                )
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                generation_wal[wal_fields["failureClass"]] = failure_class
                generation_wal[wal_fields["failureReason"]] = failure_reason
                generation_wal[wal_fields["updatedAt"]] = timestamp
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
                raise _generation_failure_stop(
                    failure_class,
                    failure_reason,
                    rules,
                    {
                        "taskId": generation_task[task_fields["taskIdentity"]],
                        "providerRequestId": generation_submission[
                            submission_fields["providerRequestIdentity"]
                        ],
                    },
                )
            generation_wal[wal_fields["pollAttemptCount"]] += 1
            generation_wal[wal_fields["updatedAt"]] = timestamp
            _write_generation_wal(generation_wal_path, generation_wal, rules)
            _record_artifact(
                manifest,
                output_dir,
                generation_wal_name,
                p2,
                [generation_task_name],
            )
            _persist_manifest(output_dir, manifest)
            poll_generation = getattr(adapters, "poll_generation", None)
            if not callable(poll_generation):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "生成 adapter 缺少 queued poll seam。",
                    {"operation": "poll_generation"},
                )
            poll_result = _adapter_snapshot_image_object_call(
                rules,
                "poll_generation",
                poll_generation,
                source_image,
                source_sha,
                package_request,
                task_request,
                submission_request,
            )
        if (
            package_request != generation_package
            or task_request != generation_task
            or submission_request != generation_submission
            or not _generation_poll_shape_valid(poll_result, generation_task, rules)
        ):
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "生成轮询结果未绑定冻结任务、提交凭证或合法输出。",
                {},
            )
        if poll_result[poll_fields["status"]] == execution_contract["pollStatuses"][
            "failed"
        ]:
            failure_class = poll_result[poll_fields["failureClass"]]
            failure_reason = poll_result[poll_fields["failureReason"]]
            failure_reason = _sanitize_generation_failure_reason(failure_reason, rules)
            failure_role = next(
                role
                for role, value in execution_contract["failureClasses"].items()
                if value == failure_class
            )
            if generation_wal[wal_fields["pollAttemptCount"]] >= execution_contract[
                "retryBudgets"
            ][failure_role] > 0:
                failure_class = execution_contract["failureClasses"]["permanent"]
                failure_reason = "retry budget exhausted: " + failure_reason
            generation_wal[wal_fields["status"]] = execution_contract["walStatuses"][
                "failed"
            ]
            generation_wal[wal_fields["failureClass"]] = failure_class
            generation_wal[wal_fields["failureReason"]] = failure_reason
            generation_wal[wal_fields["updatedAt"]] = timestamp
            _write_generation_wal(generation_wal_path, generation_wal, rules)
            _record_artifact(
                manifest,
                output_dir,
                generation_wal_name,
                p2,
                [generation_task_name],
            )
            _persist_manifest(output_dir, manifest)
            raise _generation_failure_stop(
                failure_class,
                failure_reason,
                rules,
                {
                    "taskId": generation_task[task_fields["taskIdentity"]],
                    "providerRequestId": generation_submission[
                        submission_fields["providerRequestIdentity"]
                    ],
                },
            )
        generated_extension = poll_result[poll_fields["extension"]]
        if re.fullmatch(rules["identifiers"]["imageExtensionPattern"], generated_extension) is None:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "生成适配器返回了不安全的图片扩展名。",
                {"extension": generated_extension},
            )
        candidate_rel = _revisioned_name(
            f"evidence/generated-candidate-image{generated_extension}", manifest["revision"]
        )
        candidate_path = output_dir / candidate_rel
        _atomic_write_new(candidate_path, poll_result[poll_fields["imageBytes"]])
        _record_artifact(
            manifest,
            output_dir,
            candidate_rel,
            p2,
            [generation_package_name, generation_task_name, generation_wal_name],
        )
        generation_wal[wal_fields["status"]] = execution_contract["walStatuses"][
            "succeeded"
        ]
        generation_wal[wal_fields["providerOutputIdentity"]] = poll_result[
            poll_fields["providerOutputIdentity"]
        ]
        generation_wal[wal_fields["outputSha256"]] = _sha_file(candidate_path)
        generation_wal[wal_fields["outputAssets"]] = poll_result[
            poll_fields["outputAssets"]
        ]
        generation_wal[wal_fields["failureClass"]] = None
        generation_wal[wal_fields["failureReason"]] = None
        generation_wal[wal_fields["updatedAt"]] = timestamp
        _write_generation_wal(generation_wal_path, generation_wal, rules)
        _record_artifact(
            manifest,
            output_dir,
            generation_wal_name,
            p2,
            [generation_task_name],
        )
        _persist_manifest(output_dir, manifest)
        review_bindings = {
            "generatedImageSha256": _sha_file(candidate_path),
            "generationPackageSha256": _sha_bytes(_canonical_bytes(generation_package)),
        }
        operation_request_field = rules["multiInstanceContract"]["generationFields"][
            "imageOperations"
        ]
        review_request = {
            "bindings": copy.deepcopy(review_bindings),
            operation_request_field: copy.deepcopy(
                generation_package[operation_request_field]
            ),
        }
        review_request_snapshot = copy.deepcopy(review_request)
        review = _adapter_snapshot_image_object_call(
            rules,
            "inspect_generated",
            adapters.inspect_generated,
            candidate_path,
            review_bindings["generatedImageSha256"],
            review_request,
        )
        candidate_unchanged = _sha_file(candidate_path) == review_bindings["generatedImageSha256"]
        review_request_unchanged = review_request == review_request_snapshot
        gate_stop = _evaluate_visual_gate(
            review,
            rules,
            review_bindings,
            identity_text_required=(
                rules["identityReplacementContract"]["planFields"]["route"] in plan
            ),
            expected_image_operations=plan[
                rules["multiInstanceContract"]["planFields"]["imageOperations"]
            ],
        )
        if not candidate_unchanged or not review_request_unchanged:
            if isinstance(review, dict):
                review["decision"] = rules["visualReviewContract"]["decisionValues"]["rejected"]
                review["decisionEvidence"] = {
                    "candidateBytesUnchanged": candidate_unchanged,
                    "reviewRequestUnchanged": review_request_unchanged,
                }
            gate_stop = _stop(
                rules,
                "failed",
                "externalFailure",
                "视觉审核期间候选图或审核请求发生变化，审核绑定已失效。",
                {"path": candidate_rel},
            )
        review_name = _revisioned_name("visual-review.json", manifest["revision"])
        _atomic_write_new(output_dir / review_name, _json_bytes(review))
        _record_artifact(manifest, output_dir, review_name, p2, [candidate_rel, generation_package_name])
        if gate_stop is not None:
            raise gate_stop
        approved_rel = _revisioned_name(
            f"evidence/approved-template-image{generated_extension}", manifest["revision"]
        )
        approved_path = output_dir / approved_rel
        _atomic_write_new(approved_path, candidate_path.read_bytes())
        _record_artifact(manifest, output_dir, approved_rel, p2, [candidate_rel, review_name])
        _advance(manifest, rules, p2, timestamp)
        _persist_manifest(output_dir, manifest)

        approved_sha = _sha_file(approved_path)
        analysis = _adapter_snapshot_image_object_call(
            rules,
            "analyze_approved",
            adapters.analyze_approved,
            approved_path,
            approved_sha,
        )
        if analysis.get("visualFactSourceSha256") != approved_sha:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "模板分析修改了确认模板图或未绑定视觉审核通过的图片摘要。",
                {"approvedImageSha256": approved_sha},
            )
        _atomic_write_new(output_dir / "template-analysis.json", _json_bytes(analysis))
        _record_artifact(manifest, output_dir, "template-analysis.json", p3, [approved_rel, review_name])
        _advance(manifest, rules, p3, timestamp)
        _persist_manifest(output_dir, manifest)

        editable = _compile_editable_spec(analysis, rules, plan)
        _atomic_write_new(output_dir / "editable-template-spec.json", _json_bytes(editable))
        _record_artifact(manifest, output_dir, "editable-template-spec.json", p4, ["template-analysis.json"])
        _advance(manifest, rules, p4, timestamp)
        _persist_manifest(output_dir, manifest)

        hidden = _compile_hidden_spec(analysis, editable, rules)
        _atomic_write_new(output_dir / "hidden-template-spec.json", _json_bytes(hidden))
        _record_artifact(manifest, output_dir, "hidden-template-spec.json", p5, ["template-analysis.json", "editable-template-spec.json"])
        draft = _compile_draft(
            template_key,
            source_analysis.get("imageSize", "1024x1024"),
            editable,
            hidden,
            rules,
        )
        _atomic_write_new(output_dir / "gallery-template.draft.json", _json_bytes(draft))
        _record_artifact(manifest, output_dir, "gallery-template.draft.json", p5, ["editable-template-spec.json", "hidden-template-spec.json"])
        _advance(manifest, rules, p5, timestamp)
        _persist_manifest(output_dir, manifest)

        semantic_audit_content = _semantic_audit_payload(draft, editable, rules)
        compiled_content_sha = _sha_bytes(_canonical_bytes(semantic_audit_content))
        semantic_audit_request = copy.deepcopy(semantic_audit_content)
        audit_request_sha = _sha_bytes(_canonical_bytes(semantic_audit_request))
        semantic_audit = _adapter_object_call(
            rules,
            "audit_semantics",
            adapters.audit_semantics,
            semantic_audit_request,
        )
        compiled_content_unchanged = (
            _sha_bytes(_canonical_bytes(_semantic_audit_payload(draft, editable, rules)))
            == compiled_content_sha
        )
        audit_request_unchanged = (
            _sha_bytes(_canonical_bytes(semantic_audit_request)) == audit_request_sha
        )
        if not compiled_content_unchanged or not audit_request_unchanged:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "语义审计 adapter 修改了只读编译快照。",
                {
                    "compiledContentUnchanged": compiled_content_unchanged,
                    "auditRequestUnchanged": audit_request_unchanged,
                },
            )
        _atomic_write_new(output_dir / "semantic-audit.json", _json_bytes(semantic_audit))
        _record_artifact(
            manifest,
            output_dir,
            "semantic-audit.json",
            p6,
            ["gallery-template.draft.json", "editable-template-spec.json"],
        )
        validation = _validation_report(
            draft,
            editable,
            plan,
            source_analysis,
            review,
            semantic_audit,
            rules,
        )
        _atomic_write_new(output_dir / "validation-report.json", _json_bytes(validation))
        _record_artifact(
            manifest,
            output_dir,
            "validation-report.json",
            p6,
            ["gallery-template.draft.json", review_name, "semantic-audit.json"],
        )
        if not validation["pass"]:
            raise _stop(rules, "blocked", "contractFailure", "四层静态验收未通过。", validation)
        _advance(manifest, rules, p6, timestamp)
        _persist_manifest(output_dir, manifest)

        delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
        if delivery_errors:
            raise _stop(
                rules,
                "blocked",
                "productionItemIntegrityFailure",
                "上传前候选图与确认模板图谱系不一致。",
                {"errors": delivery_errors},
            )
        object_key = _object_storage_key(
            template_key,
            delivery["approvedPath"],
            delivery["approvedSha256"],
            rules,
        )
        receipt_path = output_dir / "asset-receipt.json"
        if receipt_path.exists():
            receipt = _load_json(receipt_path)
            if not _asset_receipt_valid(
                receipt, manifest, delivery, object_key, rules
            ):
                raise _stop(
                    rules,
                    "blocked",
                    "productionItemIntegrityFailure",
                    "已有 Asset Receipt 与当前确认模板图或对象键不一致。",
                    {"path": str(receipt_path)},
                )
        else:
            upload_result = _adapter_snapshot_image_object_call(
                rules,
                "upload",
                adapters.upload,
                delivery["approvedPath"],
                delivery["approvedSha256"],
                object_key,
            )
            if not _upload_result_valid(
                upload_result, object_key, delivery["approvedSha256"], rules
            ):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "上传结果未绑定确认模板图、远端对象或请求身份。",
                    {},
                )
            receipt = _build_asset_receipt(
                manifest, delivery, upload_result, rules
            )
            _atomic_write_new(receipt_path, _json_bytes(receipt))
        _record_artifact(
            manifest,
            output_dir,
            "asset-receipt.json",
            p7,
            [delivery["approvedName"], "validation-report.json"],
        )
        _advance(manifest, rules, p7, timestamp)
        _persist_manifest(output_dir, manifest)

        receipt_fields = rules["objectStorageContract"]["receiptFields"]
        final_record = _formal_projection(
            draft, receipt[receipt_fields["url"]], rules
        )
        final_validation = _validate_final(final_record, rules)
        _atomic_write_new(output_dir / "final-validation-report.json", _json_bytes(final_validation))
        _record_artifact(manifest, output_dir, "final-validation-report.json", p8, ["gallery-template.draft.json", "asset-receipt.json"])
        if not final_validation["pass"]:
            raise _stop(rules, "blocked", "contractFailure", "正式 JSON 最终合同验证未通过。", final_validation)
        _atomic_write_new(output_dir / "gallery-template.json", _json_bytes(final_record))
        _record_artifact(manifest, output_dir, "gallery-template.json", p8, ["gallery-template.draft.json", "asset-receipt.json", "final-validation-report.json"])
        _advance(manifest, rules, p8, timestamp)
        manifest["outcome"] = "completed"
        _persist_manifest(output_dir, manifest)
        return ProductionResult(
            "completed",
            item_id,
            rules["resultStates"]["completed"],
            output_dir,
            output_dir / "gallery-template.json",
            resumed=resumed,
        )
    except WorkflowStop as stop:
        manifest["state"] = stop.state
        manifest["outcome"] = stop.outcome
        manifest["error"] = {"code": stop.error_code, "message": stop.message, "evidence": stop.evidence}
        _persist_manifest(output_dir, manifest)
        return ProductionResult(
            stop.outcome,
            item_id,
            stop.state,
            output_dir,
            error_code=stop.error_code,
            message=stop.message,
            resumed=resumed,
        )
