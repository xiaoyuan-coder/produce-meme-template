from __future__ import annotations

import copy
import io
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json as _load_json,
    pretty_json_bytes as _json_bytes,
    sha256_bytes as _sha_bytes,
    sha256_file as _sha_file,
)
from .workflow_core import (
    WorkflowStop,
    _atomic_write_new,
    _persist_manifest,
    _record_artifact,
    _revision_image_artifacts,
    _revisioned_name,
    _stop,
    _write_generation_wal,
)


def _compile_generation_package(
    plan: dict[str, Any], source_analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    target = plan["primaryTargets"][0]
    dependency_value_field = rules["identityReplacementContract"]["dependencyFields"][
        "description"
    ]
    sections = {
        "task": "基于参考资产完成整图重构，输出一张可独立使用的新模板图。",
        "replacementTarget": f"将{target['sourceRole']}完整替换为{target['replacementValue']}。",
        "dependencyClosure": "；".join(
            item[dependency_value_field] for item in plan["dependencyClosure"]
        ),
        "frozenSet": "；".join(plan["frozenSet"]),
        "mediumContract": "；".join(f"{key}: {value}" for key, value in source_analysis["visualContract"].items()),
        "residueCleanup": "清理旧身份特征、旧轮廓、水印、签名、平台标和账户标。",
        "spatialRelations": "；".join(source_analysis["spatialRelations"]),
        "output": "保持完整画布与原比例，清晰输出，不新增文字。",
    }
    context_contract = rules["sourceAuthoringContextContract"]
    cultural = source_analysis[context_contract["culturalReferenceField"]]
    continuity = source_analysis[context_contract["subjectContinuityField"]]
    cultural_fields = context_contract["culturalReferenceFields"]
    reference_fields = context_contract["referenceFields"]
    references = cultural[cultural_fields["references"]]
    cultural_values = [
        f"{reference[reference_fields['name']]}({reference[reference_fields['role']]})"
        for reference in references
    ]
    sections["culturalReference"] = (
        "IP/文化身份结论: "
        + cultural[cultural_fields["status"]]
        + "；已确认锚点: "
        + ("、".join(cultural_values) if cultural_values else "无")
        + "；候选: "
        + ("、".join(cultural[cultural_fields["candidates"]]) or "无")
    )
    continuity_fields = context_contract["subjectContinuityFields"]
    sections["subjectContinuity"] = "；".join(
        [
            f"主体数量: {continuity[continuity_fields['subjectCount']]}",
            f"物种/类型: {continuity[continuity_fields['speciesOrType']]}",
            f"性别呈现: {continuity[continuity_fields['genderPresentation']]}",
            f"年龄阶段: {continuity[continuity_fields['apparentAge']]}",
            f"服装角色: {continuity[continuity_fields['outfitRole']]}",
            f"反差机制: {continuity[continuity_fields['contrastMechanism']]}",
            "必须保持: "
            + "、".join(continuity[continuity_fields["preserveTraits"]]),
        ]
    )
    identity_contract = rules["identityReplacementContract"]
    plan_fields = identity_contract["planFields"]
    if plan_fields["route"] in plan:
        section_roles = identity_contract["generationSectionRoles"]
        route_evidence = plan[plan_fields["route"]]
        card = target.get(identity_contract["candidateFields"]["card"])
        route_parts = [
            f"mode: {route_evidence[identity_contract['routeEvidenceFields']['mode']]}",
            "完整重绘人物与全部身份依赖",
        ]
        if isinstance(card, dict):
            card_fields = identity_contract["candidateCardFields"]
            route_parts.extend(
                [
                    "身份锚点: " + "、".join(card[card_fields["anchors"]]),
                    "反锚点: " + "、".join(card[card_fields["antiAnchors"]]),
                    "玩法融合: " + "、".join(card[card_fields["playFusion"]]),
                ]
            )
        sections[section_roles["route"]] = "；".join(route_parts)
        decision_fields = identity_contract["identityTextDecisionFields"]
        sections[section_roles["identityText"]] = "；".join(
            f"{item[decision_fields['sourceText']]} -> "
            f"{item[decision_fields['action']]} -> {item[decision_fields['result']]}"
            for item in plan[plan_fields["textDecisions"]]
        )
    request_id = "gen-" + _sha_bytes(_canonical_bytes({"plan": plan, "sections": sections}))[:24]
    multi_contract = rules["multiInstanceContract"]
    return {
        "artifactType": "generation-package",
        "schemaVersion": plan["schemaVersion"],
        "requestId": request_id,
        "sections": sections,
        multi_contract["generationFields"]["imageOperations"]: copy.deepcopy(
            plan[multi_contract["planFields"]["imageOperations"]]
        ),
        "output": {"imageCount": 1, "size": source_analysis.get("imageSize", "1024x1024")},
        "replacementPlanSha256": _sha_bytes(_json_bytes(plan)),
    }


def _compile_redo_generation_package(
    previous_package: dict[str, Any],
    previous_review: dict[str, Any],
    revision: int,
) -> dict[str, Any]:
    package = copy.deepcopy(previous_package)
    correction = {
        "revision": revision,
        "failedGates": previous_review.get("decisionEvidence", {}).get("failedGates", []),
        "previousGenerationPackageSha256": _sha_bytes(_canonical_bytes(previous_package)),
        "previousVisualReviewSha256": _sha_bytes(_canonical_bytes(previous_review)),
    }
    package["redo"] = correction
    package["requestId"] = "gen-" + _sha_bytes(
        _canonical_bytes({"previousRequestId": previous_package["requestId"], "correction": correction})
    )[:24]
    return package


def _compile_generation_task(
    generation_package: dict[str, Any],
    source_sha256: str,
    production_pin_sha256: str,
    revision: int,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["generationExecutionContract"]
    task_fields = contract["taskFields"]
    intent_fields = contract["requestIntentFields"]
    option_fields = contract["requestOptionFields"]
    generation_package_sha = _sha_bytes(_json_bytes(generation_package))
    request_intent = {
        intent_fields["generationRequestIdentity"]: generation_package["requestId"],
        intent_fields["prompt"]: "\n".join(generation_package["sections"].values()),
        intent_fields["imageCount"]: generation_options[option_fields["imageCount"]],
        intent_fields["primaryOutputIndex"]: generation_options[
            option_fields["primaryOutputIndex"]
        ],
        intent_fields["imageSize"]: generation_package["output"]["size"],
        intent_fields["outputFormat"]: contract["outputFormats"]["png"],
    }
    request_intent_sha = _sha_bytes(_canonical_bytes(request_intent))
    input_sha = _sha_bytes(
        _canonical_bytes(
            {
                task_fields["sourceImageSha256"]: source_sha256,
                task_fields["generationPackageSha256"]: generation_package_sha,
                task_fields["productionPinSha256"]: production_pin_sha256,
                task_fields["requestIntentSha256"]: request_intent_sha,
            }
        )
    )
    identity_payload = {
        task_fields["revision"]: revision,
        task_fields["inputSha256"]: input_sha,
        task_fields["requestIntentSha256"]: request_intent_sha,
    }
    task_id = (
        contract["artifactTypes"]["task"]
        + "-"
        + _sha_bytes(_canonical_bytes(identity_payload))[:24]
    )
    return {
        task_fields["artifactType"]: contract["artifactTypes"]["task"],
        task_fields["schemaVersion"]: rules["schemaVersion"],
        task_fields["taskIdentity"]: task_id,
        task_fields["revision"]: revision,
        task_fields["sourceImageSha256"]: source_sha256,
        task_fields["generationPackageSha256"]: generation_package_sha,
        task_fields["productionPinSha256"]: production_pin_sha256,
        task_fields["inputSha256"]: input_sha,
        task_fields["requestIntent"]: request_intent,
        task_fields["requestIntentSha256"]: request_intent_sha,
    }


def _prepared_generation_wal(
    generation_task: dict[str, Any], timestamp: str, rules: dict[str, Any]
) -> dict[str, Any]:
    contract = rules["generationExecutionContract"]
    task_fields = contract["taskFields"]
    wal_fields = contract["walFields"]
    return {
        wal_fields["artifactType"]: contract["artifactTypes"]["wal"],
        wal_fields["schemaVersion"]: rules["schemaVersion"],
        wal_fields["taskIdentity"]: generation_task[task_fields["taskIdentity"]],
        wal_fields["taskSha256"]: _sha_bytes(_json_bytes(generation_task)),
        wal_fields["previousWalSha256"]: None,
        wal_fields["revision"]: generation_task[task_fields["revision"]],
        wal_fields["status"]: contract["walStatuses"]["prepared"],
        wal_fields["provider"]: None,
        wal_fields["model"]: None,
        wal_fields["providerRequestIdentity"]: None,
        wal_fields["providerOutputIdentity"]: None,
        wal_fields["outputSha256"]: None,
        wal_fields["outputAssets"]: [],
        wal_fields["pollAttemptCount"]: 0,
        wal_fields["failureClass"]: None,
        wal_fields["failureReason"]: None,
        wal_fields["updatedAt"]: timestamp,
    }


def _generation_failure_stop(
    failure_class: str,
    failure_reason: str,
    rules: dict[str, Any],
    evidence: dict[str, Any],
) -> WorkflowStop:
    contract = rules["generationExecutionContract"]
    failure_reason = _sanitize_generation_failure_reason(failure_reason, rules)
    failure_role = next(
        (
            role
            for role, value in contract["failureClasses"].items()
            if value == failure_class
        ),
        "permanent",
    )
    route = contract["failureRoutes"][failure_role]
    recovery_phase_index = route["recoveryPhaseIndex"]
    routed_evidence = {
        **evidence,
        "failureClass": failure_class,
        "failureReason": failure_reason,
        "recoverablePhase": (
            rules["productionPhases"][recovery_phase_index]["phase"]
            if recovery_phase_index is not None
            else None
        ),
    }
    return _stop(
        rules,
        route["outcomeRole"],
        route["errorCodeRole"],
        "生成任务未完成，已按 failure class 路由到稳定恢复阶段。",
        routed_evidence,
    )


def _sanitize_generation_failure_reason(value: Any, rules: dict[str, Any]) -> str:
    contract = rules["generationExecutionContract"]["persistedErrorSanitization"]
    detail = value if isinstance(value, str) else type(value).__name__
    return contract["digestPrefix"] + _sha_bytes(detail.encode("utf-8"))[
        : contract["digestLength"]
    ]


def _generation_submission_shape_valid(
    submission: dict[str, Any], rules: dict[str, Any]
) -> bool:
    contract = rules["generationExecutionContract"]
    fields = contract["submissionFields"]
    if set(submission) != set(fields.values()):
        return False
    status = submission.get(fields["status"])
    if status == contract["submissionStatuses"]["submitted"]:
        return bool(
            _execution_identity_valid(
                submission.get(fields["provider"]),
                contract["providerIdentityPattern"],
            )
            and _execution_identity_valid(
                submission.get(fields["model"]), contract["modelIdentityPattern"]
            )
            and _execution_identity_valid(
                submission.get(fields["providerRequestIdentity"]),
                contract["opaqueExecutionIdentityPattern"],
            )
            and submission.get(fields["failureClass"]) is None
            and submission.get(fields["failureReason"]) is None
        )
    if status == contract["submissionStatuses"]["failed"]:
        return bool(
            _execution_identity_valid(
                submission.get(fields["provider"]),
                contract["providerIdentityPattern"],
            )
            and _execution_identity_valid(
                submission.get(fields["model"]), contract["modelIdentityPattern"]
            )
            and submission.get(fields["providerRequestIdentity"]) is None
            and submission.get(fields["failureClass"])
            in contract["failureClasses"].values()
            and isinstance(submission.get(fields["failureReason"]), str)
            and submission[fields["failureReason"]].strip()
        )
    return False


def _execution_identity_valid(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _generation_output_assets_valid(
    output_assets: Any,
    expected_count: int,
    asset_fields: dict[str, str],
    output_identity_pattern: str,
) -> bool:
    if not isinstance(output_assets, list) or len(output_assets) != expected_count:
        return False
    output_identities: list[str] = []
    for asset in output_assets:
        if not (
            isinstance(asset, dict)
            and set(asset) == set(asset_fields.values())
            and _execution_identity_valid(
                asset.get(asset_fields["providerOutputIdentity"]),
                output_identity_pattern,
            )
            and isinstance(asset.get(asset_fields["sha256"]), str)
            and re.fullmatch(r"[0-9a-f]{64}", asset[asset_fields["sha256"]])
        ):
            return False
        output_identities.append(asset[asset_fields["providerOutputIdentity"]])
    return len(output_identities) == len(set(output_identities))


def _image_bytes_match_output_format(
    payload: Any, output_format: str, contract: dict[str, Any]
) -> bool:
    if not isinstance(payload, bytes) or not payload:
        return False
    format_role = next(
        (role for role, value in contract["outputFormats"].items() if value == output_format),
        None,
    )
    if format_role is None or not payload.startswith(
        bytes.fromhex(contract["outputFormatSignatures"][format_role])
    ):
        return False
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if (
                image.format != contract["outputFormatDecoderNames"][format_role]
                or getattr(image, "n_frames", 1) != 1
            ):
                return False
            width, height = image.size
            image.verify()
        with Image.open(io.BytesIO(payload)) as decoded:
            decoded.load()
        return bool(
            1 <= width <= contract["maximumDecodedImageDimension"]
            and 1 <= height <= contract["maximumDecodedImageDimension"]
            and width * height <= contract["maximumDecodedImagePixels"]
        )
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
        return False


def image_bytes_match_output_format(
    payload: Any, output_format: str, contract: dict[str, Any]
) -> bool:
    """Validate a generated single-image payload against the shared machine contract."""

    return _image_bytes_match_output_format(payload, output_format, contract)


def _generation_poll_shape_valid(
    poll_result: dict[str, Any], generation_task: dict[str, Any], rules: dict[str, Any]
) -> bool:
    contract = rules["generationExecutionContract"]
    fields = contract["pollResultFields"]
    if set(poll_result) != set(fields.values()):
        return False
    status = poll_result.get(fields["status"])
    if status == contract["pollStatuses"]["failed"]:
        return bool(
            poll_result.get(fields["failureClass"])
            in contract["failureClasses"].values()
            and isinstance(poll_result.get(fields["failureReason"]), str)
            and poll_result[fields["failureReason"]].strip()
            and poll_result.get(fields["extension"]) is None
            and poll_result.get(fields["imageBytes"]) is None
            and poll_result.get(fields["providerOutputIdentity"]) is None
            and poll_result.get(fields["outputAssets"]) == []
        )
    if status != contract["pollStatuses"]["succeeded"]:
        return False
    task_fields = contract["taskFields"]
    intent_fields = contract["requestIntentFields"]
    asset_fields = contract["outputAssetFields"]
    output_assets = poll_result.get(fields["outputAssets"])
    image_bytes = poll_result.get(fields["imageBytes"])
    image_count = generation_task[task_fields["requestIntent"]][
        intent_fields["imageCount"]
    ]
    primary_index = generation_task[task_fields["requestIntent"]][
        intent_fields["primaryOutputIndex"]
    ]
    output_format = generation_task[task_fields["requestIntent"]][
        intent_fields["outputFormat"]
    ]
    return bool(
        poll_result.get(fields["failureClass"]) is None
        and poll_result.get(fields["failureReason"]) is None
        and isinstance(poll_result.get(fields["extension"]), str)
        and poll_result[fields["extension"]]
        == contract["outputFormatExtensions"][
            next(
                role
                for role, value in contract["outputFormats"].items()
                if value
                == generation_task[task_fields["requestIntent"]][
                    intent_fields["outputFormat"]
                ]
            )
        ]
        and isinstance(image_bytes, bytes)
        and image_bytes
        and _image_bytes_match_output_format(image_bytes, output_format, contract)
        and _execution_identity_valid(
            poll_result.get(fields["providerOutputIdentity"]),
            contract["opaqueExecutionIdentityPattern"],
        )
        and _generation_output_assets_valid(
            output_assets,
            image_count,
            asset_fields,
            contract["opaqueExecutionIdentityPattern"],
        )
        and output_assets[primary_index][asset_fields["providerOutputIdentity"]]
        == poll_result[fields["providerOutputIdentity"]]
        and output_assets[primary_index][asset_fields["sha256"]]
        == _sha_bytes(image_bytes)
    )


def _generation_task_wal_errors(
    generation_task: Any,
    wal: Any,
    generation_package: dict[str, Any],
    source_sha256: str,
    production_pin_sha256: str,
    revision: int,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> list[str]:
    contract = rules["generationExecutionContract"]
    task_fields = contract["taskFields"]
    wal_fields = contract["walFields"]
    errors: list[str] = []
    if not isinstance(generation_task, dict) or set(generation_task) != set(
        task_fields.values()
    ):
        return ["generation task shape invalid"]
    expected_task = _compile_generation_task(
        generation_package,
        source_sha256,
        production_pin_sha256,
        revision,
        generation_options,
        rules,
    )
    if generation_task != expected_task:
        return ["generation task identity mismatch"]
    if not isinstance(wal, dict) or set(wal) != set(wal_fields.values()):
        errors.append("generation WAL shape invalid")
        return errors
    if wal.get(wal_fields["taskIdentity"]) != generation_task[task_fields["taskIdentity"]]:
        errors.append("generation WAL task identity mismatch")
    if wal.get(wal_fields["taskSha256"]) != _sha_bytes(_json_bytes(generation_task)):
        errors.append("generation WAL task digest mismatch")
    if wal.get(wal_fields["revision"]) != revision:
        errors.append("generation WAL revision mismatch")
    if wal.get(wal_fields["status"]) not in contract["walStatuses"].values():
        errors.append("generation WAL status invalid")
    poll_attempt_count = wal.get(wal_fields["pollAttemptCount"])
    if (
        not isinstance(poll_attempt_count, int)
        or isinstance(poll_attempt_count, bool)
        or poll_attempt_count < 0
    ):
        errors.append("generation WAL poll attempt count invalid")
    if not isinstance(wal.get(wal_fields["updatedAt"]), str) or not wal[
        wal_fields["updatedAt"]
    ].strip():
        errors.append("generation WAL timestamp invalid")
    status = wal.get(wal_fields["status"])
    previous_wal_sha = wal.get(wal_fields["previousWalSha256"])
    provider_values = [
        wal.get(wal_fields[role])
        for role in ("provider", "model", "providerRequestIdentity")
    ]
    output_assets = wal.get(wal_fields["outputAssets"])
    retry_budget = contract["retryBudgets"]["retryable"]
    if status == contract["walStatuses"]["prepared"]:
        if previous_wal_sha is not None:
            errors.append("prepared generation WAL has previous digest")
        if any(value is not None for value in provider_values):
            errors.append("prepared generation WAL has provider credentials")
        if (
            wal.get(wal_fields["providerOutputIdentity"]) is not None
            or wal.get(wal_fields["outputSha256"]) is not None
            or output_assets != []
            or wal.get(wal_fields["failureClass"]) is not None
            or wal.get(wal_fields["failureReason"]) is not None
        ):
            errors.append("prepared generation WAL has terminal evidence")
        if poll_attempt_count != 0:
            errors.append("prepared generation WAL poll count invalid")
    elif status in {
        contract["walStatuses"]["submitted"],
        contract["walStatuses"]["succeeded"],
    }:
        if not isinstance(previous_wal_sha, str) or re.fullmatch(
            r"[0-9a-f]{64}", previous_wal_sha
        ) is None:
            errors.append("generation WAL previous digest invalid")
        if not (
            _execution_identity_valid(
                provider_values[0], contract["providerIdentityPattern"]
            )
            and _execution_identity_valid(
                provider_values[1], contract["modelIdentityPattern"]
            )
            and _execution_identity_valid(
                provider_values[2], contract["opaqueExecutionIdentityPattern"]
            )
        ):
            errors.append("submitted generation WAL provider credentials invalid")
        if (
            wal.get(wal_fields["failureClass"]) is not None
            or wal.get(wal_fields["failureReason"]) is not None
        ):
            errors.append("active generation WAL has failure evidence")
        if status == contract["walStatuses"]["submitted"] and not (
            isinstance(poll_attempt_count, int) and 0 <= poll_attempt_count <= retry_budget
        ):
            errors.append("submitted generation WAL poll count invalid")
        if status == contract["walStatuses"]["succeeded"] and not (
            isinstance(poll_attempt_count, int) and 1 <= poll_attempt_count <= retry_budget
        ):
            errors.append("succeeded generation WAL poll count invalid")
    elif status == contract["walStatuses"]["failed"]:
        if not isinstance(previous_wal_sha, str) or re.fullmatch(
            r"[0-9a-f]{64}", previous_wal_sha
        ) is None:
            errors.append("generation WAL previous digest invalid")
        failure_class = wal.get(wal_fields["failureClass"])
        provider, model, provider_request_id = provider_values
        provider_model_valid = bool(
            (provider is None and model is None)
            or (
                _execution_identity_valid(provider, contract["providerIdentityPattern"])
                and _execution_identity_valid(model, contract["modelIdentityPattern"])
            )
        )
        if failure_class not in contract["failureClasses"].values():
            errors.append("failed generation WAL failure class invalid")
        if not isinstance(wal.get(wal_fields["failureReason"]), str) or not wal[
            wal_fields["failureReason"]
        ].strip():
            errors.append("failed generation WAL reason invalid")
        if not provider_model_valid or (
            provider_request_id is not None
            and (
                not _execution_identity_valid(
                    provider_request_id, contract["opaqueExecutionIdentityPattern"]
                )
                or provider is None
                or model is None
            )
        ):
            errors.append("failed generation WAL provider evidence invalid")
        if (
            failure_class == contract["failureClasses"]["retryable"]
            and not (
                provider_model_valid
                and _execution_identity_valid(
                    provider_request_id, contract["opaqueExecutionIdentityPattern"]
                )
            )
        ):
            errors.append("retryable generation WAL request identity missing")
        if (
            failure_class == contract["failureClasses"]["submissionUnknown"]
            and provider_request_id is not None
        ):
            errors.append("unknown submission WAL cannot claim a request identity")
        if failure_class == contract["failureClasses"]["submissionUnknown"]:
            if poll_attempt_count != 0:
                errors.append("unknown submission WAL poll count invalid")
        elif failure_class == contract["failureClasses"]["retryable"]:
            if not (
                isinstance(poll_attempt_count, int)
                and 1 <= poll_attempt_count < retry_budget
            ):
                errors.append("retryable generation WAL poll count invalid")
        elif not (
            isinstance(poll_attempt_count, int) and 0 <= poll_attempt_count <= retry_budget
        ):
            errors.append("failed generation WAL poll count invalid")
    if status != contract["walStatuses"]["succeeded"]:
        if (
            wal.get(wal_fields["providerOutputIdentity"]) is not None
            or wal.get(wal_fields["outputSha256"]) is not None
            or output_assets != []
        ):
            errors.append("unfinished generation WAL has output evidence")
    else:
        intent_fields = contract["requestIntentFields"]
        asset_fields = contract["outputAssetFields"]
        intent = generation_task[task_fields["requestIntent"]]
        if (
            not _generation_output_assets_valid(
                output_assets,
                intent[intent_fields["imageCount"]],
                asset_fields,
                contract["opaqueExecutionIdentityPattern"],
            )
        ):
            errors.append("succeeded generation WAL output assets invalid")
        if not _execution_identity_valid(
            wal.get(wal_fields["providerOutputIdentity"]),
            contract["opaqueExecutionIdentityPattern"],
        ):
            errors.append("succeeded generation WAL output identity invalid")
        if not isinstance(wal.get(wal_fields["outputSha256"]), str) or re.fullmatch(
            r"[0-9a-f]{64}", wal[wal_fields["outputSha256"]]
        ) is None:
            errors.append("succeeded generation WAL output digest invalid")
        if isinstance(output_assets, list) and len(output_assets) == intent[
            intent_fields["imageCount"]
        ]:
            primary_asset = output_assets[intent[intent_fields["primaryOutputIndex"]]]
            if isinstance(primary_asset, dict) and (
                primary_asset.get(asset_fields["providerOutputIdentity"])
                != wal.get(wal_fields["providerOutputIdentity"])
                or primary_asset.get(asset_fields["sha256"])
                != wal.get(wal_fields["outputSha256"])
            ):
                errors.append("generation WAL primary output mismatch")
    return errors


def _load_generation_execution_evidence(
    output_dir: Path,
    package_name: str,
    task_name: str,
    wal_name: str,
    source_sha256: str,
    revision: int,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> tuple[list[str], Any, Any, Any]:
    try:
        generation_package = _load_json(output_dir / package_name)
        generation_task = _load_json(output_dir / task_name)
        generation_wal = _load_json(output_dir / wal_name)
        errors = _generation_task_wal_errors(
            generation_task,
            generation_wal,
            generation_package,
            source_sha256,
            _sha_file(output_dir / "production-pin.json"),
            revision,
            generation_options,
            rules,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["generation execution evidence unreadable"], None, None, None
    return errors, generation_package, generation_task, generation_wal


def _adopt_pre_submit_generation_staging(
    output_dir: Path,
    manifest: dict[str, Any],
    source_sha256: str,
    generation_options: dict[str, int],
    rules: dict[str, Any],
    timestamp: str,
    phase: str,
) -> tuple[list[str], Any, Any, Any]:
    """Validate and register an interrupted, provider-side-effect-free P2 staging set."""
    revision = manifest.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return ["generation staging revision invalid"], None, None, None
    package_name = _revisioned_name("generation-package.json", revision)
    task_name = _revisioned_name("generation-task.json", revision)
    wal_name = _revisioned_name("generation-wal.json", revision)
    package_path = output_dir / package_name
    task_path = output_dir / task_name
    wal_path = output_dir / wal_name
    try:
        source_analysis = _load_json(output_dir / "source-analysis.json")
        plan = _load_json(output_dir / "replacement-plan.json")
        expected_package = _compile_generation_package(plan, source_analysis, rules)
        contract = rules["generationExecutionContract"]
        expected_package["output"]["imageCount"] = generation_options[
            contract["requestOptionFields"]["imageCount"]
        ]
        if package_path.is_file():
            generation_package = _load_json(package_path)
            if generation_package != expected_package:
                return ["untracked generation package mismatch"], None, None, None
        else:
            generation_package = expected_package
            _atomic_write_new(package_path, _json_bytes(generation_package))
        expected_task = _compile_generation_task(
            generation_package,
            source_sha256,
            _sha_file(output_dir / "production-pin.json"),
            revision,
            generation_options,
            rules,
        )
        if task_path.is_file():
            generation_task = _load_json(task_path)
            if generation_task != expected_task:
                return ["untracked generation task mismatch"], None, None, None
        else:
            generation_task = expected_task
            _atomic_write_new(task_path, _json_bytes(generation_task))
        if wal_path.is_file():
            generation_wal = _load_json(wal_path)
        else:
            generation_wal = _prepared_generation_wal(
                generation_task, timestamp, rules
            )
            _write_generation_wal(wal_path, generation_wal, rules)
        errors = _generation_task_wal_errors(
            generation_task,
            generation_wal,
            generation_package,
            source_sha256,
            _sha_file(output_dir / "production-pin.json"),
            revision,
            generation_options,
            rules,
        )
        wal_fields = contract["walFields"]
        if generation_wal.get(wal_fields["status"]) != contract["walStatuses"][
            "prepared"
        ]:
            errors.append("untracked generation WAL is not prepared")
        if errors:
            return errors, generation_package, generation_task, generation_wal
        _record_artifact(
            manifest, output_dir, package_name, phase, ["replacement-plan.json"]
        )
        _record_artifact(
            manifest,
            output_dir,
            task_name,
            phase,
            [package_name, "production-pin.json"],
        )
        _record_artifact(manifest, output_dir, wal_name, phase, [task_name])
        _persist_manifest(output_dir, manifest)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["generation staging evidence unreadable"], None, None, None
    return [], generation_package, generation_task, generation_wal


def _current_generation_execution_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    source_sha256: str,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> list[str]:
    revision = manifest.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return ["generation execution revision invalid"]
    package_name = _revisioned_name("generation-package.json", revision)
    task_name = _revisioned_name("generation-task.json", revision)
    wal_name = _revisioned_name("generation-wal.json", revision)
    candidate_names = _revision_image_artifacts(
        manifest, "generated-candidate-image", revision
    )
    if len(candidate_names) != 1:
        return ["current generated candidate count must be one"]
    required_paths = {
        package_name: output_dir / package_name,
        task_name: output_dir / task_name,
        wal_name: output_dir / wal_name,
        "production-pin.json": output_dir / "production-pin.json",
        candidate_names[0]: output_dir / candidate_names[0],
    }
    missing = [name for name, path in required_paths.items() if not path.is_file()]
    if missing:
        return [f"generation execution artifact missing: {name}" for name in missing]
    errors, generation_package, generation_task, generation_wal = (
        _load_generation_execution_evidence(
            output_dir,
            package_name,
            task_name,
            wal_name,
            source_sha256,
            revision,
            generation_options,
            rules,
        )
    )
    if errors:
        return errors
    contract = rules["generationExecutionContract"]
    wal_fields = contract["walFields"]
    if generation_wal[wal_fields["status"]] != contract["walStatuses"]["succeeded"]:
        errors.append("current generation WAL is not succeeded")
    candidate_sha = _sha_file(required_paths[candidate_names[0]])
    if generation_wal[wal_fields["outputSha256"]] != candidate_sha:
        errors.append("generation WAL candidate digest mismatch")
    task_fields = contract["taskFields"]
    intent_fields = contract["requestIntentFields"]
    output_format = generation_task[task_fields["requestIntent"]][
        intent_fields["outputFormat"]
    ]
    output_format_role = next(
        role
        for role, value in contract["outputFormats"].items()
        if value == output_format
    )
    candidate_path = required_paths[candidate_names[0]]
    if (
        candidate_path.suffix != contract["outputFormatExtensions"][output_format_role]
        or not _image_bytes_match_output_format(
            candidate_path.read_bytes(), output_format, contract
        )
    ):
        errors.append("generation candidate format mismatch")
    return errors


def _evaluate_visual_gate(
    review: Any,
    rules: dict[str, Any],
    expected_bindings: dict[str, str],
    identity_text_required: bool,
    expected_image_operations: list[dict[str, Any]],
) -> WorkflowStop | None:
    if not isinstance(review, dict):
        return _stop(
            rules,
            "failed",
            "externalFailure",
            "视觉审核证据必须是对象。",
            {"actualType": type(review).__name__},
        )
    contract = rules["visualReviewContract"]
    hard_gate_names = set(contract["hardGateRoles"].values())
    cleanliness_names = set(contract["cleanlinessFindingRoles"].values())
    ambiguity_names = set(contract["ambiguitySignalRoles"].values())
    evidence_fields = contract["evidenceFieldRoles"]
    hard_gates = review.get(evidence_fields["hardGates"])
    dimensions = review.get(evidence_fields["visualDimensions"])
    visible_text = review.get(evidence_fields["visibleText"])
    cleanliness = review.get(evidence_fields["cleanliness"])
    ambiguities = review.get(evidence_fields["ambiguity"])
    identity_text_field = evidence_fields["identityText"]
    identity_text = review.get(identity_text_field)
    identity_text_fields = contract["identityTextEvidenceFields"]
    multi_contract = rules["multiInstanceContract"]
    operation_fields = multi_contract["operationFields"]
    operation_review_fields = multi_contract["operationReviewFields"]
    operation_evidence = review.get(evidence_fields["imageOperations"])
    expected_operation_ids = {
        operation[operation_fields["identity"]] for operation in expected_image_operations
    }
    bindings = review.get("bindings")
    method = review.get("method")
    evidence_payload = (
        {field: review[field] for field in evidence_fields.values()}
        if all(field in review for field in evidence_fields.values())
        else None
    )
    contract_valid = bool(
        review.get("artifactType") == "visual-review"
        and review.get("schemaVersion") == rules["schemaVersion"]
        and isinstance(hard_gates, dict)
        and set(hard_gates) == hard_gate_names
        and all(isinstance(value, bool) for value in hard_gates.values())
        and isinstance(dimensions, dict)
        and set(dimensions) == set(rules["visualDimensions"])
        and all(
            isinstance(value, dict)
            and isinstance(value.get("pass"), bool)
            and isinstance(value.get("evidence"), str)
            and value.get("evidence").strip()
            for value in dimensions.values()
        )
        and isinstance(visible_text, dict)
        and isinstance(visible_text.get("pass"), bool)
        and isinstance(visible_text.get("evidence"), str)
        and visible_text.get("evidence").strip()
        and isinstance(cleanliness, dict)
        and set(cleanliness) == cleanliness_names
        and all(isinstance(value, bool) for value in cleanliness.values())
        and isinstance(ambiguities, dict)
        and set(ambiguities) == ambiguity_names
        and all(isinstance(value, bool) for value in ambiguities.values())
        and isinstance(identity_text, dict)
        and set(identity_text) == set(identity_text_fields.values())
        and identity_text.get(identity_text_fields["applicability"]) is identity_text_required
        and isinstance(identity_text.get(identity_text_fields["legacyTermsAbsent"]), bool)
        and isinstance(identity_text.get(identity_text_fields["replacementConsistency"]), bool)
        and isinstance(identity_text.get(identity_text_fields["explanation"]), str)
        and identity_text[identity_text_fields["explanation"]].strip()
        and isinstance(operation_evidence, list)
        and len(operation_evidence) == len(expected_operation_ids)
        and all(
            isinstance(item, dict)
            and set(item) == set(operation_review_fields.values())
            and isinstance(
                item.get(operation_review_fields["operationIdentity"]), str
            )
            and item.get(operation_review_fields["operationIdentity"])
            in expected_operation_ids
            and all(
                isinstance(item.get(operation_review_fields[field]), bool)
                for field in (
                    "targetCleared",
                    "anchorsStable",
                    "relationsPreserved",
                    "nonTargetStable",
                )
            )
            and isinstance(item.get(operation_review_fields["explanation"]), str)
            and item[operation_review_fields["explanation"]].strip()
            for item in operation_evidence
        )
        and len(
            {
                item[operation_review_fields["operationIdentity"]]
                for item in operation_evidence
            }
        )
        == len(operation_evidence)
        and isinstance(bindings, dict)
        and all(bindings.get(key) == value for key, value in expected_bindings.items())
        and evidence_payload is not None
        and bindings.get("evidenceSha256") == _sha_bytes(_canonical_bytes(evidence_payload))
        and isinstance(method, dict)
        and isinstance(method.get("id"), str)
        and method.get("id").strip()
        and isinstance(method.get("version"), str)
        and method.get("version").strip()
        and isinstance(review.get("reviewedAt"), str)
        and review.get("reviewedAt").strip()
    )
    if not contract_valid:
        review["decision"] = contract["decisionValues"]["rejected"]
        review["decisionEvidence"] = {"contractValid": False}
        return _stop(
            rules,
            "failed",
            "externalFailure",
            "视觉审核证据合同无效或未绑定当前生图事实。",
            {"expectedBindings": expected_bindings},
        )
    failures = [name for name, passed in hard_gates.items() if passed is not True]
    failures.extend(name for name, value in dimensions.items() if value["pass"] is not True)
    failures.extend(name for name, found in cleanliness.items() if found is True)
    if visible_text["pass"] is not True:
        failures.append(contract["hardGateRoles"]["visibleText"])
    if identity_text_required and (
        identity_text[identity_text_fields["legacyTermsAbsent"]] is not True
        or identity_text[identity_text_fields["replacementConsistency"]] is not True
    ):
        failures.append(contract["hardGateRoles"]["visibleText"])
        failures.append(contract["hardGateRoles"]["legacyIdentityAbsence"])
    for item in operation_evidence:
        if item[operation_review_fields["targetCleared"]] is not True:
            failures.append(contract["hardGateRoles"]["dependencyClosure"])
        if item[operation_review_fields["anchorsStable"]] is not True:
            failures.append(contract["hardGateRoles"]["nonTargetPreservation"])
        if item[operation_review_fields["relationsPreserved"]] is not True:
            failures.append(contract["hardGateRoles"]["contactGeometry"])
        if item[operation_review_fields["nonTargetStable"]] is not True:
            failures.append(contract["hardGateRoles"]["nonTargetPreservation"])
    if failures:
        failed_gates = sorted(set(failures))
        review["decision"] = contract["decisionValues"]["rejected"]
        review["decisionEvidence"] = {"failedGates": failed_gates}
        return _stop(
            rules,
            "blocked",
            "visualHardFailure",
            "生成图未通过模板图视觉硬门禁，必须修正或重生成。",
            {"failedGates": failed_gates},
        )
    review_signals = sorted(name for name, present in ambiguities.items() if present is True)
    if review_signals:
        review["decision"] = contract["decisionValues"]["needsReview"]
        review["decisionEvidence"] = {"reviewSignals": review_signals}
        return _stop(
            rules,
            "needs_input",
            "riskNeedsReview",
            "生成图存在歧义、审美风险或证据不足，需要人工复核。",
            {"reviewSignals": review_signals},
        )
    review["decision"] = contract["decisionValues"]["approved"]
    review["decisionEvidence"] = {"hardGatesPassed": True}
    return None
