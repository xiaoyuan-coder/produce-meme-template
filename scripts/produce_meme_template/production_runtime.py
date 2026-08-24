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
from .authoring_handoff import (
    authoring_handoff_valid,
    compile_authoring_handoff,
    compile_authoring_intent,
    source_authoring_context_errors,
)
from .execution_authority import (
    qualify_runtime_execution_profile,
)
from .batch_policy import (
    _isolated_output_dir,
    _normalize_replacement_strategy,
    _normalized_generation_options,
    _production_request_errors,
    _source_analysis_identity_valid,
)
from .delivery_runtime import (
    _current_finalization_errors,
    _current_item_fact_errors,
    _current_shared_policy_resolution_errors,
    _current_template_data_errors,
    _finalize_uploaded_item,
    _run_finalization_stage,
)
from .delivery_qualification import p8_completion_qualification_errors
from .generation_runtime import (
    _adopt_pre_submit_generation_staging,
    _compile_generation_package,
    _compile_generation_task,
    _compile_redo_generation_package,
    _current_generation_execution_errors,
    _evaluate_visual_gate,
    _generation_failure_stop,
    _generation_poll_shape_valid,
    _generation_submission_shape_valid,
    _load_generation_execution_evidence,
    _prepared_generation_wal,
    _sanitize_generation_failure_reason,
)
from .replacement_planning import _build_pin, _plan_replacement
from .production_gates import (
    authoring_contract_audit_errors,
    compile_authoring_review_request,
    template_identity_resolution_errors,
)
from .template_compiler import (
    _compile_draft,
    _compile_editable_spec,
    _compile_hidden_spec,
    _semantic_audit_payload,
    _validation_report,
)
from .workflow_core import (
    GALLERY_SCHEMA_PATH,
    RELEASE_PATH,
    REPO_ROOT,
    RULES_PATH,
    ProductionResult,
    WorkflowAdapters,
    WorkflowStop,
    _adapter_object_call,
    _adapter_snapshot_image_object_call,
    _advance,
    _append_invalidation_event,
    _artifact_descendants,
    _atomic_write_new,
    _changed_lineage_artifacts,
    _current_p2_artifact_errors,
    _file_matches_sha,
    _persist_manifest,
    _production_item_integrity_errors,
    _record_artifact,
    _revision_image_artifacts,
    _revisioned_name,
    _stop,
    _write_generation_wal,
)


def _major_stage_definition(rules: dict[str, Any], stage_number: int) -> dict[str, Any]:
    return next(
        stage
        for stage in rules["majorStageContract"]["stages"]
        if stage["number"] == stage_number
    )


def _stage_package(
    manifest: dict[str, Any],
    rules: dict[str, Any],
    *,
    artifact_type_role: str,
    artifact_roles: dict[str, str],
    status: str | None = None,
) -> dict[str, Any]:
    package = {
        "artifactType": rules["majorStageContract"]["packageArtifactTypes"][
            artifact_type_role
        ],
        "schemaVersion": rules["schemaVersion"],
        "productionItemId": manifest["productionItemId"],
        "templateKey": manifest["templateKey"],
        "revision": manifest["revision"],
        "artifacts": {
            role: {
                "path": name,
                "sha256": manifest["artifacts"][name]["sha256"],
            }
            for role, name in artifact_roles.items()
        },
    }
    if status is not None:
        package["status"] = status
    return package


def _completed_stage_result(
    manifest: dict[str, Any],
    output_dir: Path,
    rules: dict[str, Any],
    stage_number: int,
    primary_artifact: Path,
    *,
    resumed: bool,
) -> ProductionResult:
    stage = _major_stage_definition(rules, stage_number)
    return ProductionResult(
        "completed",
        manifest["productionItemId"],
        manifest["state"],
        output_dir,
        output_dir / "gallery-template.json" if stage_number == 4 else None,
        resumed=resumed,
        major_stage=stage["selector"],
        primary_artifact=primary_artifact,
    )


def _run_template_data_stage(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
    adapters: WorkflowAdapters,
    template_key: str,
    source_analysis: dict[str, Any],
    plan: dict[str, Any],
    timestamp: str,
    target_stage: int,
    *,
    resumed: bool,
) -> ProductionResult:
    p3, p4, p5, p6 = (item["phase"] for item in rules["productionPhases"][3:7])
    revision = manifest["revision"]
    review_name = _revisioned_name("visual-review.json", revision)
    approved_names = _revision_image_artifacts(
        manifest, "approved-template-image", revision
    )
    if len(approved_names) != 1:
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "第三阶段要求当前 revision 恰有一张 Approved Template Image。",
            {"approvedArtifacts": approved_names},
        )
    approved_rel = approved_names[0]
    approved_path = output_dir / approved_rel
    approved_sha = _sha_file(approved_path)
    handoff_name = rules["authoringHandoffContract"]["artifactNames"]["handoff"]
    handoff_path = output_dir / handoff_name
    if not handoff_path.is_file():
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "第三阶段缺少与 Approved Template Image 绑定的 Authoring Handoff。",
            {"artifact": handoff_name},
        )
    authoring_handoff = _load_json(handoff_path)
    intent_name = rules["authoringHandoffContract"]["artifactNames"]["intent"]
    generation_package_name = _revisioned_name(
        "generation-package.json", revision
    )
    authoring_intent = _load_json(output_dir / intent_name)
    generation_package = _load_json(output_dir / generation_package_name)
    review = _load_json(output_dir / review_name)
    expected_handoff_bindings = {
        "sourceImageSha256": source_analysis["sourceImageSha256"],
        "sourceAnalysisSha256": _sha_bytes(_canonical_bytes(source_analysis)),
        "replacementPlanSha256": _sha_bytes(_canonical_bytes(plan)),
        "generationPackageSha256": _sha_bytes(
            _canonical_bytes(generation_package)
        ),
        "authoringIntentSha256": _sha_bytes(_canonical_bytes(authoring_intent)),
        "visualReviewSha256": _sha_bytes(_canonical_bytes(review)),
        "approvedImageSha256": approved_sha,
    }
    if (
        not authoring_handoff_valid(authoring_handoff, approved_sha, rules)
        or authoring_handoff.get("bindings") != expected_handoff_bindings
    ):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "Authoring Handoff 的结构或 Approved Image 绑定无效。",
            {"artifact": handoff_name},
        )
    handoff_request = copy.deepcopy(authoring_handoff)
    handoff_sha = _sha_bytes(_canonical_bytes(handoff_request))
    analyze_with_handoff = getattr(adapters, "analyze_approved_with_handoff", None)
    if not callable(analyze_with_handoff):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "模板分析 adapter 缺少 Authoring Handoff seam。",
            {"operation": "analyze_approved_with_handoff"},
        )
    analysis = _adapter_snapshot_image_object_call(
        rules,
        "analyze_approved_with_handoff",
        analyze_with_handoff,
        approved_path,
        approved_sha,
        handoff_request,
    )
    if (
        analysis.get("visualFactSourceSha256") != approved_sha
        or _sha_bytes(_canonical_bytes(handoff_request)) != handoff_sha
    ):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "模板分析修改了确认图或 Authoring Handoff，或未绑定当前图片摘要。",
            {"approvedImageSha256": approved_sha},
        )
    _atomic_write_new(output_dir / "template-analysis.json", _json_bytes(analysis))
    _record_artifact(
        manifest,
        output_dir,
        "template-analysis.json",
        p3,
        [approved_rel, review_name, handoff_name],
    )
    _advance(manifest, rules, p3, timestamp)
    _persist_manifest(output_dir, manifest)

    authoring_review_request = compile_authoring_review_request(
        analysis, approved_sha, authoring_handoff, rules
    )
    authoring_request_sha = _sha_bytes(
        _canonical_bytes(authoring_review_request)
    )
    audit_authoring_contract = getattr(
        adapters, "audit_authoring_contract", None
    )
    if not callable(audit_authoring_contract):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "模板分析 adapter 缺少独立 Authoring Contract Audit seam。",
            {"operation": "audit_authoring_contract"},
        )
    authoring_audit = _adapter_snapshot_image_object_call(
        rules,
        "audit_authoring_contract",
        audit_authoring_contract,
        approved_path,
        approved_sha,
        authoring_review_request,
    )
    if _sha_bytes(_canonical_bytes(authoring_review_request)) != authoring_request_sha:
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "作者合同审计 adapter 修改了只读对账快照。",
            {},
        )
    authoring_audit_name = rules["authoringContractAudit"]["artifactName"]
    _atomic_write_new(
        output_dir / authoring_audit_name, _json_bytes(authoring_audit)
    )
    _record_artifact(
        manifest,
        output_dir,
        authoring_audit_name,
        p4,
        ["template-analysis.json", approved_rel, handoff_name],
    )
    authoring_errors = authoring_contract_audit_errors(
        authoring_audit, authoring_review_request, rules
    )
    if authoring_errors:
        _persist_manifest(output_dir, manifest)
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "独立作者合同审计未通过。",
            {"errors": authoring_errors},
        )

    editable = _compile_editable_spec(
        analysis, rules, plan, authoring_audit, authoring_handoff
    )
    _atomic_write_new(
        output_dir / "editable-template-spec.json", _json_bytes(editable)
    )
    _record_artifact(
        manifest,
        output_dir,
        "editable-template-spec.json",
        p4,
        ["template-analysis.json", authoring_audit_name, handoff_name],
    )
    _advance(manifest, rules, p4, timestamp)
    _persist_manifest(output_dir, manifest)

    hidden = _compile_hidden_spec(analysis, editable, rules)
    _atomic_write_new(output_dir / "hidden-template-spec.json", _json_bytes(hidden))
    _record_artifact(
        manifest,
        output_dir,
        "hidden-template-spec.json",
        p5,
        ["template-analysis.json", "editable-template-spec.json"],
    )
    draft = _compile_draft(
        template_key,
        source_analysis.get("imageSize", "1024x1024"),
        editable,
        hidden,
        rules,
    )
    _atomic_write_new(output_dir / "gallery-template.draft.json", _json_bytes(draft))
    _record_artifact(
        manifest,
        output_dir,
        "gallery-template.draft.json",
        p5,
        ["editable-template-spec.json", "hidden-template-spec.json"],
    )
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
    grounding_contract = rules["visualContractGroundingReviewContract"]
    grounding_request_fields = grounding_contract["requestFields"]
    runtime_fields = rules["runtimeSemanticsContract"]["fields"]
    formal_runtime_field = rules["formalProjection"]["topLevel"][
        "runtimeSemantics"
    ]
    grounding_request = {
        grounding_request_fields["approvedImageSha256"]: approved_sha,
        grounding_request_fields["visualContract"]: copy.deepcopy(
            draft[formal_runtime_field][runtime_fields["visualContract"]]
        ),
        grounding_request_fields["renderingCoherenceDecision"]: copy.deepcopy(
            editable[
                rules["renderingCoherenceDecisionContract"]["authoringField"]
            ]
        ),
        grounding_request_fields["componentGraph"]: copy.deepcopy(
            editable[
                rules["multiInstanceContract"]["approvedFields"][
                    "componentGraph"
                ]
            ]
        ),
    }
    grounding_request_sha = _sha_bytes(_canonical_bytes(grounding_request))
    visual_contract_grounding_review = _adapter_snapshot_image_object_call(
        rules,
        "audit_visual_contract",
        adapters.audit_visual_contract,
        approved_path,
        approved_sha,
        grounding_request,
    )
    if _sha_bytes(_canonical_bytes(grounding_request)) != grounding_request_sha:
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "视觉合同审计 adapter 修改了只读对账快照。",
            {},
        )
    semantic_evidence = semantic_audit.get("evidence")
    grounding_evidence_field = grounding_contract["evidenceField"]
    if (
        isinstance(semantic_evidence, dict)
        and grounding_evidence_field not in semantic_evidence
    ):
        semantic_evidence[grounding_evidence_field] = visual_contract_grounding_review
    compiled_content_unchanged = (
        _sha_bytes(
            _canonical_bytes(_semantic_audit_payload(draft, editable, rules))
        )
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
        [
            "gallery-template.draft.json",
            "editable-template-spec.json",
            approved_rel,
        ],
    )
    review = _load_json(output_dir / review_name)
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
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "四层静态验收未通过。",
            validation,
        )
    _advance(manifest, rules, p6, timestamp)
    data_name = rules["majorStageContract"]["artifactNames"][
        "templateDataPackage"
    ]
    data_roles = {
        "approvedTemplateImage": approved_rel,
        "authoringHandoff": handoff_name,
        "templateAnalysis": "template-analysis.json",
        "authoringContractAudit": authoring_audit_name,
        "editableTemplateSpec": "editable-template-spec.json",
        "runtimeSemanticsSpec": "hidden-template-spec.json",
        "formalDraft": "gallery-template.draft.json",
        "semanticAudit": "semantic-audit.json",
        "validationReport": "validation-report.json",
    }
    data_package = _stage_package(
        manifest,
        rules,
        artifact_type_role="templateDataPackage",
        artifact_roles=data_roles,
        status=rules["majorStageContract"]["templateDataStatus"],
    )
    _atomic_write_new(output_dir / data_name, _json_bytes(data_package))
    _record_artifact(
        manifest,
        output_dir,
        data_name,
        p6,
        list(data_roles.values()),
    )
    _persist_manifest(output_dir, manifest)
    if target_stage == 3:
        return _completed_stage_result(
            manifest,
            output_dir,
            rules,
            3,
            output_dir / data_name,
            resumed=resumed,
        )
    return _run_finalization_stage(
        output_dir,
        manifest,
        rules,
        adapters,
        template_key,
        timestamp,
        p8_completion_qualification_errors,
        resumed=resumed,
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
    target_stage: int = 4,
    execution_profile: dict[str, Any] | None = None,
) -> ProductionResult:
    """Run one Production Item through the requested resumable major stage."""

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
    if execution_profile is None:
        return ProductionResult(
            "blocked",
            item_id,
            rules["resultStates"]["blocked"],
            output_dir,
            error_code=rules["errorCodes"]["untrustedProductionExecution"],
            message="缺少工作流预检生成的正式执行画像。",
            resumed=manifest_path.is_file(),
        )
    execution_profile, diagnostic_errors, execution_errors = (
        qualify_runtime_execution_profile(
            execution_profile,
            rules,
            production_pin=existing_pin,
        )
    )
    if diagnostic_errors:
        return ProductionResult(
            "blocked",
            item_id,
            rules["resultStates"]["blocked"],
            output_dir,
            error_code=rules["errorCodes"]["versionDiagnosticFailure"],
            message="运行前 doctor 检查未通过："
            + "、".join(diagnostic_errors),
            resumed=manifest_path.is_file(),
        )
    if execution_errors:
        return ProductionResult(
            "blocked",
            item_id,
            rules["resultStates"]["blocked"],
            output_dir,
            error_code=rules["errorCodes"]["untrustedProductionExecution"],
            message="正式执行画像未通过运行时校验：" + "；".join(execution_errors),
            resumed=manifest_path.is_file(),
        )
    production_execution_contract = rules["productionExecutionContract"]
    execution_profile_name = production_execution_contract["artifactName"]
    execution_profile_sha = _sha_bytes(_json_bytes(execution_profile))
    resume_visual = False
    resume_generation = False
    resume_prepared_generation = False
    resume_from_stage_one = False
    resume_template_data = False
    resume_finalization = False
    reuse_succeeded_generation = False
    resumed = False
    source_analysis: dict[str, Any]
    plan: dict[str, Any]
    generation_package: dict[str, Any]
    authoring_intent: dict[str, Any]
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
        completed_artifacts = (
            execution_profile_name,
            "production-pin.json",
            "gallery-template.json",
            "final-validation-report.json",
        )
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
        try:
            persisted_execution_profile = _load_json(
                output_dir / execution_profile_name
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            persisted_execution_profile = None
        if (
            existing.get(
                production_execution_contract["manifestFields"]["executionMode"]
            )
            != execution_profile[
                production_execution_contract["profileFields"]["executionMode"]
            ]
            or existing.get(
                production_execution_contract["manifestFields"][
                    "executionProfileSha256"
                ]
            )
            != execution_profile_sha
            or persisted_execution_profile != execution_profile
            or existing.get("artifacts", {})
            .get(execution_profile_name, {})
            .get("sha256")
            != execution_profile_sha
        ):
            identity_errors.append("production execution profile mismatch")
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
            identity_errors.extend(
                _current_template_data_errors(output_dir, existing, rules)
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
            if target_stage == 1:
                completed_primary = output_dir / rules["majorStageContract"][
                    "artifactNames"
                ]["replacementPackage"]
            elif target_stage == 2:
                completed_primary = output_dir / _revision_image_artifacts(
                    existing,
                    "approved-template-image",
                    existing["revision"],
                )[0]
            elif target_stage == 3:
                completed_primary = output_dir / rules["majorStageContract"][
                    "artifactNames"
                ]["templateDataPackage"]
            else:
                completed_primary = output_dir / "gallery-template.json"
            return _completed_stage_result(
                existing,
                output_dir,
                rules,
                target_stage,
                completed_primary,
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
                    execution_profile_name,
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
            recovery_errors.extend(
                _current_template_data_errors(output_dir, existing, rules)
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
            if target_stage <= 3:
                if target_stage == 1:
                    checkpoint_primary = output_dir / rules["majorStageContract"][
                        "artifactNames"
                    ]["replacementPackage"]
                elif target_stage == 2:
                    checkpoint_primary = output_dir / _revision_image_artifacts(
                        existing,
                        "approved-template-image",
                        existing["revision"],
                    )[0]
                else:
                    checkpoint_primary = output_dir / rules["majorStageContract"][
                        "artifactNames"
                    ]["templateDataPackage"]
                return _completed_stage_result(
                    existing,
                    output_dir,
                    rules,
                    target_stage,
                    checkpoint_primary,
                    resumed=True,
                )
            try:
                return _finalize_uploaded_item(
                    output_dir,
                    existing,
                    rules,
                    adapters,
                    timestamp,
                    p8_completion_qualification_errors,
                )
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
        checkpoint_by_phase = {
            p1: 1,
            p2: 2,
            p6: 3,
        }
        checkpoint_stage = checkpoint_by_phase.get(existing.get("phase"))
        expected_checkpoint_state = next(
            (
                phase["state"]
                for phase in rules["productionPhases"]
                if phase["phase"] == existing.get("phase")
            ),
            None,
        )
        if (
            checkpoint_stage is not None
            and existing.get("state") == expected_checkpoint_state
            and existing.get("outcome") is None
            and not existing.get("error")
        ):
            revision = existing.get("revision")
            generation_package_name = _revisioned_name(
                "generation-package.json", revision
            )
            replacement_name = rules["majorStageContract"]["artifactNames"][
                "replacementPackage"
            ]
            data_name = rules["majorStageContract"]["artifactNames"][
                "templateDataPackage"
            ]
            required_by_stage = {
                1: (
                    execution_profile_name,
                    "production-pin.json",
                    "source-analysis.json",
                    "replacement-plan.json",
                    generation_package_name,
                    rules["authoringHandoffContract"]["artifactNames"]["intent"],
                    replacement_name,
                ),
                2: (
                    execution_profile_name,
                    "production-pin.json",
                    "source-analysis.json",
                    "replacement-plan.json",
                    generation_package_name,
                    rules["authoringHandoffContract"]["artifactNames"]["intent"],
                    rules["authoringHandoffContract"]["artifactNames"]["handoff"],
                    replacement_name,
                ),
                3: (
                    execution_profile_name,
                    "production-pin.json",
                    "source-analysis.json",
                    "replacement-plan.json",
                    generation_package_name,
                    rules["authoringHandoffContract"]["artifactNames"]["intent"],
                    rules["authoringHandoffContract"]["artifactNames"]["handoff"],
                    replacement_name,
                    "gallery-template.draft.json",
                    "validation-report.json",
                    data_name,
                ),
            }
            checkpoint_errors = _production_item_integrity_errors(
                output_dir,
                existing,
                production_item_id=item_id,
                template_key=template_key,
                source_sha256=source_sha,
                replacement_strategy_sha256=replacement_strategy_sha,
                generation_options_sha256=generation_options_sha,
                required_artifacts=required_by_stage[checkpoint_stage],
            )
            if checkpoint_stage >= 2:
                checkpoint_errors.extend(_current_p2_artifact_errors(existing))
                checkpoint_errors.extend(
                    _current_generation_execution_errors(
                        output_dir,
                        existing,
                        source_sha,
                        generation_options,
                        rules,
                    )
                )
            if checkpoint_errors:
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message="大阶段恢复前的 Production Item 谱系校验失败："
                    + "；".join(checkpoint_errors),
                    resumed=True,
                )
            manifest = existing
            source_analysis = _load_json(output_dir / "source-analysis.json")
            plan = _load_json(output_dir / "replacement-plan.json")
            resumed = True
            if target_stage <= checkpoint_stage:
                if target_stage == 1:
                    primary_artifact = output_dir / replacement_name
                elif target_stage == 2:
                    approved_names = _revision_image_artifacts(
                        manifest, "approved-template-image", manifest["revision"]
                    )
                    primary_artifact = output_dir / approved_names[0]
                else:
                    primary_artifact = output_dir / data_name
                return _completed_stage_result(
                    manifest,
                    output_dir,
                    rules,
                    target_stage,
                    primary_artifact,
                    resumed=True,
                )
            if checkpoint_stage == 1:
                generation_package = _load_json(
                    output_dir / generation_package_name
                )
                resume_from_stage_one = True
            elif checkpoint_stage == 2:
                resume_template_data = True
            else:
                resume_finalization = True
        existing_error_code = existing.get("error", {}).get("code")
        existing_revision = existing.get("revision")
        if (
            isinstance(existing_revision, int)
            and existing_error_code
            != rules["errorCodes"]["visualHardFailure"]
            and not resume_template_data
            and not resume_finalization
        ):
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
                            execution_profile_name,
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
                        execution_profile_name,
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
            redo_field = rules["generationExecutionContract"][
                "requestControlFields"
            ]["authorizeVisualRedo"]
            if request.get(redo_field) is not True:
                return ProductionResult(
                    existing.get("outcome", "blocked"),
                    item_id,
                    existing.get("state", rules["resultStates"]["blocked"]),
                    output_dir,
                    error_code=rules["errorCodes"]["visualHardFailure"],
                    message=(
                        "当前生产项已经消耗一次新供应商请求；视觉重做需要显式设置 "
                        f"{redo_field}=true。"
                    ),
                    resumed=True,
                )
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
                        execution_profile_name,
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
                invalidated_artifacts=[
                    name
                    for name in _artifact_descendants(
                        manifest, previous_package_name
                    )
                    if name
                    != rules["majorStageContract"]["artifactNames"][
                        "replacementPackage"
                    ]
                ],
                invalidated_from_phase=p2,
                timestamp=timestamp,
            )
            resume_visual = True
            resumed = True
            _persist_manifest(output_dir, manifest)
    if not any(
        (
            resume_visual,
            resume_generation,
            resume_prepared_generation,
            resume_from_stage_one,
            resume_template_data,
            resume_finalization,
        )
    ):
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
            production_execution_contract["manifestFields"]["executionMode"]: execution_profile[
                production_execution_contract["profileFields"]["executionMode"]
            ],
            production_execution_contract["manifestFields"][
                "executionProfileSha256"
            ]: execution_profile_sha,
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
        if resume_finalization:
            return _run_finalization_stage(
                output_dir,
                manifest,
                rules,
                adapters,
                template_key,
                timestamp,
                p8_completion_qualification_errors,
                resumed=True,
            )
        if resume_template_data:
            return _run_template_data_stage(
                output_dir,
                manifest,
                rules,
                adapters,
                template_key,
                source_analysis,
                plan,
                timestamp,
                target_stage,
                resumed=True,
            )
        if not any(
            (
                resume_visual,
                resume_generation,
                resume_prepared_generation,
                resume_from_stage_one,
            )
        ):
            _atomic_write_new(
                output_dir / execution_profile_name,
                _json_bytes(execution_profile),
            )
            _record_artifact(
                manifest,
                output_dir,
                execution_profile_name,
                p0,
                [],
            )
            identity_contract = rules["templateIdentityContract"]
            identity_name = identity_contract["artifactName"]
            resolve_template_identity = getattr(
                adapters, "resolve_template_identity", None
            )
            if not callable(resolve_template_identity):
                raise _stop(
                    rules,
                    "blocked",
                    "templateKeyRegistryUnavailable",
                    "生产 adapter 缺少模板身份注册表查询 seam。",
                    {"operation": "resolve_template_identity"},
                )
            identity_request = {
                "productionItemId": item_id,
                "templateKey": template_key,
            }
            identity_request_sha = _sha_bytes(
                _canonical_bytes(identity_request)
            )
            identity_resolution = _adapter_snapshot_image_object_call(
                rules,
                "resolve_template_identity",
                resolve_template_identity,
                source_image,
                source_sha,
                identity_request,
            )
            if _sha_bytes(_canonical_bytes(identity_request)) != identity_request_sha:
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "模板身份 adapter 修改了只读查询请求。",
                    {},
                )
            identity_error_role, identity_errors = template_identity_resolution_errors(
                identity_resolution,
                source_sha256=source_sha,
                proposed_key=template_key,
                rules=rules,
            )
            _atomic_write_new(
                output_dir / identity_name, _json_bytes(identity_resolution)
            )
            _record_artifact(manifest, output_dir, identity_name, p0, [])
            if identity_error_role is not None:
                state_role = (
                    "needs_input"
                    if identity_error_role
                    in {
                        "templateKeyExistingMismatch",
                        "templateKeySemanticInvalid",
                        "templateKeyConflict",
                    }
                    else "blocked"
                )
                raise _stop(
                    rules,
                    state_role,
                    identity_error_role,
                    "模板身份与语义 key 门禁未通过。",
                    {"errors": identity_errors},
                )
            pin = _build_pin(rules, release)
            _atomic_write_new(output_dir / "production-pin.json", _json_bytes(pin))
            _record_artifact(
                manifest,
                output_dir,
                "production-pin.json",
                p0,
                [execution_profile_name, identity_name],
            )
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
            authoring_context_errors = source_authoring_context_errors(
                source_analysis, rules
            )
            if authoring_context_errors:
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "来源分析没有完成 IP/文化身份发现或主体连续性冻结。",
                    {"errors": authoring_context_errors},
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
            authoring_intent = compile_authoring_intent(
                source_analysis, plan, rules
            )
        execution_contract = rules["generationExecutionContract"]
        task_fields = execution_contract["taskFields"]
        wal_fields = execution_contract["walFields"]
        submission_fields = execution_contract["submissionFields"]
        poll_fields = execution_contract["pollResultFields"]
        if (
            not resume_visual
            and not resume_generation
            and not resume_prepared_generation
            and not resume_from_stage_one
        ):
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
                p1,
                ["replacement-plan.json"],
            )
            intent_name = rules["authoringHandoffContract"]["artifactNames"]["intent"]
            _atomic_write_new(
                output_dir / intent_name, _json_bytes(authoring_intent)
            )
            _record_artifact(
                manifest,
                output_dir,
                intent_name,
                p1,
                ["source-analysis.json", "replacement-plan.json"],
            )
            replacement_name = rules["majorStageContract"]["artifactNames"][
                "replacementPackage"
            ]
            replacement_roles = {
                "sourceAnalysis": "source-analysis.json",
                "replacementPlan": "replacement-plan.json",
                "generationPackage": generation_package_name,
                "authoringIntent": intent_name,
            }
            replacement_package = _stage_package(
                manifest,
                rules,
                artifact_type_role="replacementPackage",
                artifact_roles=replacement_roles,
            )
            _atomic_write_new(
                output_dir / replacement_name, _json_bytes(replacement_package)
            )
            _record_artifact(
                manifest,
                output_dir,
                replacement_name,
                p1,
                list(replacement_roles.values()),
            )
            _persist_manifest(output_dir, manifest)
            if target_stage == 1:
                return _completed_stage_result(
                    manifest,
                    output_dir,
                    rules,
                    1,
                    output_dir / replacement_name,
                    resumed=resumed,
                )
        if not resume_generation:
            if not resume_prepared_generation:
                generation_package["output"]["imageCount"] = generation_options[
                    execution_contract["requestOptionFields"]["imageCount"]
                ]
                generation_package_name = _revisioned_name(
                    "generation-package.json", manifest["revision"]
                )
                if generation_package_name not in manifest["artifacts"]:
                    _atomic_write_new(
                        output_dir / generation_package_name,
                        _json_bytes(generation_package),
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
            execution_fields = rules["productionExecutionContract"][
                "profileFields"
            ]
            if (
                manifest[
                    rules["productionExecutionContract"]["manifestFields"][
                        "executionMode"
                    ]
                ]
                == rules["productionExecutionContract"]["executionModes"][
                    "liveExternal"
                ]
                and generation_submission[submission_fields["provider"]]
                != execution_profile[execution_fields["generationProvider"]]
            ):
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                for role in ("provider", "model", "providerRequestIdentity"):
                    generation_wal[wal_fields[role]] = generation_submission[
                        submission_fields[role]
                    ]
                generation_wal[wal_fields["failureClass"]] = execution_contract[
                    "failureClasses"
                ]["submissionUnknown"]
                generation_wal[wal_fields["failureReason"]] = (
                    "provider identity does not match the trusted execution profile"
                )
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
                raise _stop(
                    rules,
                    "blocked",
                    "untrustedProductionExecution",
                    "P2 生成凭证的 provider 与正式执行画像不一致。",
                    {
                        "provider": generation_submission[
                            submission_fields["provider"]
                        ]
                    },
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
        if manifest[
            rules["productionExecutionContract"]["manifestFields"][
                "executionMode"
            ]
        ] == rules["productionExecutionContract"]["executionModes"][
            "liveExternal"
        ]:
            readiness = rules["releaseReadinessContract"]
            method = review.get(
                readiness["liveReviewEvidenceFields"]["method"]
            )
            method_identity = (
                method.get(
                    readiness["liveReviewEvidenceFields"]["methodIdentity"]
                )
                if isinstance(method, dict)
                else None
            )
            expected_method = execution_profile[
                rules["productionExecutionContract"]["profileFields"][
                    "visualReviewMethodIdentity"
                ]
            ]
            if method_identity != expected_method:
                raise _stop(
                    rules,
                    "blocked",
                    "untrustedProductionExecution",
                    "P2 视觉审核证据的方法身份与正式执行画像不一致。",
                    {"methodIdentity": method_identity},
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
        intent_name = rules["authoringHandoffContract"]["artifactNames"]["intent"]
        authoring_intent = _load_json(output_dir / intent_name)
        authoring_handoff = compile_authoring_handoff(
            authoring_intent,
            review,
            generation_package,
            _sha_file(approved_path),
            rules,
        )
        handoff_name = rules["authoringHandoffContract"]["artifactNames"]["handoff"]
        _atomic_write_new(output_dir / handoff_name, _json_bytes(authoring_handoff))
        _record_artifact(
            manifest,
            output_dir,
            handoff_name,
            p2,
            [intent_name, generation_package_name, review_name, approved_rel],
        )
        _advance(manifest, rules, p2, timestamp)
        _persist_manifest(output_dir, manifest)
        if target_stage == 2:
            return _completed_stage_result(
                manifest,
                output_dir,
                rules,
                2,
                approved_path,
                resumed=resumed,
            )
        return _run_template_data_stage(
            output_dir,
            manifest,
            rules,
            adapters,
            template_key,
            source_analysis,
            plan,
            timestamp,
            target_stage,
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
