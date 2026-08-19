from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .artifacts import load_json as _load_json, pretty_json_bytes as _json_bytes, sha256_file as _sha_file
from .batch_policy import _shared_policy_plan_valid
from .template_compiler import (
    _compile_draft,
    _compile_editable_spec,
    _compile_hidden_spec,
    _formal_projection,
    _validate_final,
)
from .workflow_core import (
    ProductionResult,
    WorkflowStop,
    _advance,
    _atomic_write_new,
    _persist_manifest,
    _public_asset_url_valid,
    _record_artifact,
    _revision_image_artifacts,
    _stop,
)


def _delivery_image_context(
    output_dir: Path, manifest: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    revision = manifest.get("revision")
    candidate_names = _revision_image_artifacts(
        manifest, "generated-candidate-image", revision
    )
    approved_names = _revision_image_artifacts(
        manifest, "approved-template-image", revision
    )
    errors: list[str] = []
    if len(candidate_names) != 1:
        errors.append("current candidate image count must be one")
    if len(approved_names) != 1:
        errors.append("current approved image count must be one")
    if errors:
        return errors, {}
    candidate_name = candidate_names[0]
    approved_name = approved_names[0]
    candidate_path = output_dir / candidate_name
    approved_path = output_dir / approved_name
    if not candidate_path.is_file() or not approved_path.is_file():
        return ["current candidate or approved image missing"], {}
    candidate_sha = _sha_file(candidate_path)
    approved_sha = _sha_file(approved_path)
    if candidate_sha != approved_sha:
        errors.append("approved image no longer matches the reviewed candidate")
    return errors, {
        "candidateName": candidate_name,
        "candidateSha256": candidate_sha,
        "approvedName": approved_name,
        "approvedPath": approved_path,
        "approvedSha256": approved_sha,
    }

def _object_storage_key(
    template_key: str, approved_path: Path, approved_sha256: str, rules: dict[str, Any]
) -> str:
    contract = rules["objectStorageContract"]
    object_key = (
        f"{contract['objectKeyPrefix']}/{template_key}/"
        f"{approved_sha256}{approved_path.suffix.lower()}"
    )
    allowed_extensions = set(
        rules["generationExecutionContract"]["outputFormatExtensions"].values()
    )
    if (
        not re.fullmatch(r"[a-z][a-z0-9/-]*", contract["objectKeyPrefix"])
        or approved_path.suffix.lower() not in allowed_extensions
        or ".." in object_key
        or object_key.startswith("/")
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "OSS 对象键不符合冻结合同。",
            {"objectKey": object_key},
        )
    return object_key

def _asset_url_matches_object_key(value: Any, object_key: str) -> bool:
    if not isinstance(value, str):
        return False
    path = unquote(urlsplit(value).path)
    return path == "/" + object_key or path.endswith("/" + object_key)

def _storage_identity_valid(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None

def _upload_result_valid(
    result: Any,
    expected_object_key: str,
    expected_image_sha256: str,
    rules: dict[str, Any],
) -> bool:
    contract = rules["objectStorageContract"]
    fields = contract["adapterResultFields"]
    return bool(
        isinstance(result, dict)
        and set(result) == set(fields.values())
        and _storage_identity_valid(
            result.get(fields["provider"]), contract["providerIdentityPattern"]
        )
        and result.get(fields["provider"]) in contract["providerRoles"].values()
        and result.get(fields["objectKey"]) == expected_object_key
        and _storage_identity_valid(
            result.get(fields["objectIdentity"]),
            contract["remoteIdentityPattern"],
        )
        and result.get(fields["imageSha256"]) == expected_image_sha256
        and _public_asset_url_valid(result.get(fields["url"]), rules)
        and _asset_url_matches_object_key(
            result.get(fields["url"]), expected_object_key
        )
        and _storage_identity_valid(
            result.get(fields["idempotencyKey"]),
            contract["idempotencyIdentityPattern"],
        )
        and result.get(fields["idempotencyKey"])
        == contract["idempotencyKeyPrefix"] + expected_image_sha256
        and result.get(fields["uploadStatus"])
        in contract["uploadStatuses"].values()
        and _storage_identity_valid(
            result.get(fields["providerRequestIdentity"]),
            contract["requestIdentityPattern"],
        )
        and isinstance(result.get(fields["providerStatusCode"]), int)
        and not isinstance(result.get(fields["providerStatusCode"]), bool)
        and 200 <= result[fields["providerStatusCode"]] < 300
    )

def _build_asset_receipt(
    manifest: dict[str, Any],
    delivery: dict[str, Any],
    upload_result: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["objectStorageContract"]
    result_fields = contract["adapterResultFields"]
    receipt_fields = contract["receiptFields"]
    return {
        receipt_fields["artifactType"]: contract["artifactType"],
        receipt_fields["schemaVersion"]: rules["schemaVersion"],
        receipt_fields["productionItemIdentity"]: manifest["productionItemId"],
        receipt_fields["templateKey"]: manifest["templateKey"],
        receipt_fields["formalRevision"]: manifest["revision"],
        receipt_fields["candidateArtifact"]: delivery["candidateName"],
        receipt_fields["candidateImageSha256"]: delivery["candidateSha256"],
        receipt_fields["approvedArtifact"]: delivery["approvedName"],
        receipt_fields["approvedImageSha256"]: delivery["approvedSha256"],
        receipt_fields["provider"]: upload_result[result_fields["provider"]],
        receipt_fields["objectKey"]: upload_result[result_fields["objectKey"]],
        receipt_fields["objectIdentity"]: upload_result[
            result_fields["objectIdentity"]
        ],
        receipt_fields["url"]: upload_result[result_fields["url"]],
        receipt_fields["idempotencyKey"]: upload_result[
            result_fields["idempotencyKey"]
        ],
        receipt_fields["uploadStatus"]: upload_result[
            result_fields["uploadStatus"]
        ],
        receipt_fields["providerRequestIdentity"]: upload_result[
            result_fields["providerRequestIdentity"]
        ],
        receipt_fields["providerStatusCode"]: upload_result[
            result_fields["providerStatusCode"]
        ],
    }

def _asset_receipt_valid(
    receipt: Any,
    manifest: dict[str, Any],
    delivery: dict[str, Any],
    expected_object_key: str,
    rules: dict[str, Any],
) -> bool:
    contract = rules["objectStorageContract"]
    fields = contract["receiptFields"]
    return bool(
        isinstance(receipt, dict)
        and set(receipt) == set(fields.values())
        and receipt.get(fields["artifactType"]) == contract["artifactType"]
        and receipt.get(fields["schemaVersion"]) == rules["schemaVersion"]
        and receipt.get(fields["productionItemIdentity"])
        == manifest["productionItemId"]
        and receipt.get(fields["templateKey"]) == manifest["templateKey"]
        and receipt.get(fields["formalRevision"]) == manifest["revision"]
        and receipt.get(fields["candidateArtifact"]) == delivery["candidateName"]
        and receipt.get(fields["candidateImageSha256"])
        == delivery["candidateSha256"]
        and receipt.get(fields["approvedArtifact"]) == delivery["approvedName"]
        and receipt.get(fields["approvedImageSha256"])
        == delivery["approvedSha256"]
        and receipt.get(fields["objectKey"]) == expected_object_key
        and _storage_identity_valid(
            receipt.get(fields["provider"]), contract["providerIdentityPattern"]
        )
        and receipt.get(fields["provider"]) in contract["providerRoles"].values()
        and _storage_identity_valid(
            receipt.get(fields["objectIdentity"]), contract["remoteIdentityPattern"]
        )
        and _public_asset_url_valid(receipt.get(fields["url"]), rules)
        and _asset_url_matches_object_key(
            receipt.get(fields["url"]), expected_object_key
        )
        and _storage_identity_valid(
            receipt.get(fields["idempotencyKey"]),
            contract["idempotencyIdentityPattern"],
        )
        and receipt.get(fields["idempotencyKey"])
        == contract["idempotencyKeyPrefix"] + delivery["approvedSha256"]
        and receipt.get(fields["uploadStatus"])
        in contract["uploadStatuses"].values()
        and _storage_identity_valid(
            receipt.get(fields["providerRequestIdentity"]),
            contract["requestIdentityPattern"],
        )
        and isinstance(receipt.get(fields["providerStatusCode"]), int)
        and not isinstance(receipt.get(fields["providerStatusCode"]), bool)
        and 200 <= receipt[fields["providerStatusCode"]] < 300
    )

def _current_finalization_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
    if delivery_errors:
        return delivery_errors
    try:
        object_key = _object_storage_key(
            manifest["templateKey"],
            delivery["approvedPath"],
            delivery["approvedSha256"],
            rules,
        )
        receipt = _load_json(output_dir / "asset-receipt.json")
        draft = _load_json(output_dir / "gallery-template.draft.json")
        record = _load_json(output_dir / "gallery-template.json")
        persisted_validation = _load_json(
            output_dir / "final-validation-report.json"
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["finalization evidence unreadable"]
    if not all(
        isinstance(item, dict)
        for item in (receipt, draft, record, persisted_validation)
    ):
        return ["finalization evidence shape invalid"]
    if not _asset_receipt_valid(
        receipt, manifest, delivery, object_key, rules
    ):
        return ["asset receipt semantic binding invalid"]
    receipt_fields = rules["objectStorageContract"]["receiptFields"]
    try:
        expected_record = _formal_projection(
            draft, receipt[receipt_fields["url"]], rules
        )
    except WorkflowStop:
        return ["formal projection source invalid"]
    expected_validation = _validate_final(expected_record, rules)
    errors: list[str] = []
    if record != expected_record:
        errors.append("formal record does not match receipt projection")
    if persisted_validation != expected_validation or not expected_validation["pass"]:
        errors.append("final validation report does not match current formal record")
    return errors

def _current_item_fact_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    try:
        source_analysis = _load_json(output_dir / "source-analysis.json")
        plan = _load_json(output_dir / "replacement-plan.json")
        template_analysis = _load_json(output_dir / "template-analysis.json")
        editable = _load_json(output_dir / "editable-template-spec.json")
        hidden = _load_json(output_dir / "hidden-template-spec.json")
        draft = _load_json(output_dir / "gallery-template.draft.json")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["production item facts unreadable"]
    if not all(
        isinstance(item, dict)
        for item in (
            source_analysis,
            plan,
            template_analysis,
            editable,
            hidden,
            draft,
        )
    ):
        return ["production item facts shape invalid"]
    errors: list[str] = []
    if source_analysis.get("sourceImageSha256") != manifest.get(
        "sourceImageSha256"
    ):
        errors.append("source analysis belongs to another source image")
    if plan.get("templateKey") != manifest.get("templateKey"):
        errors.append("replacement plan belongs to another template")
    source_target = source_analysis.get("target")
    plan_targets = plan.get("primaryTargets")
    if not (
        isinstance(source_target, dict)
        and isinstance(plan_targets, list)
        and len(plan_targets) == 1
        and isinstance(plan_targets[0], dict)
        and plan_targets[0].get("sourceCategory") == source_target.get("category")
        and plan_targets[0].get("sourceRole") == source_target.get("role")
        and plan_targets[0].get("sourceIdentity") == source_target.get("identity")
    ):
        errors.append("replacement plan does not match source facts")
    batch_contract = rules["batchProductionContract"]
    resolution_name = batch_contract["resolutionArtifactName"]
    if resolution_name in manifest.get("artifacts", {}):
        try:
            resolution = _load_json(output_dir / resolution_name)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            errors.append("shared policy resolution unreadable")
        else:
            if not _shared_policy_plan_valid(plan, resolution, rules):
                errors.append(
                    "shared policy resolution does not match replacement plan"
                )
    delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
    if delivery_errors:
        errors.extend(delivery_errors)
    elif template_analysis.get("visualFactSourceSha256") != delivery.get(
        "approvedSha256"
    ):
        errors.append("template analysis belongs to another approved image")
    try:
        expected_editable = _compile_editable_spec(
            template_analysis,
            rules,
            plan,
        )
        expected_hidden = _compile_hidden_spec(
            template_analysis,
            expected_editable,
            rules,
        )
        expected_draft = _compile_draft(
            manifest["templateKey"],
            source_analysis.get("imageSize", "1024x1024"),
            expected_editable,
            expected_hidden,
            rules,
        )
    except (KeyError, TypeError, ValueError, WorkflowStop):
        errors.append("production item facts cannot be deterministically replayed")
    else:
        if editable != expected_editable:
            errors.append("editable defaults do not match approved visual facts")
        if hidden != expected_hidden:
            errors.append("hidden template does not match approved visual facts")
        if draft != expected_draft:
            errors.append("draft does not match current item compilation")
    return errors

def _current_shared_policy_resolution_errors(
    output_dir: Path,
    expected_resolution: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    resolution_name = rules["batchProductionContract"][
        "resolutionArtifactName"
    ]
    try:
        persisted = _load_json(output_dir / resolution_name)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["shared policy resolution unreadable"]
    if persisted != expected_resolution:
        return ["shared policy resolution does not match current request"]
    return []

def _finalize_uploaded_item(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
    timestamp: str,
) -> ProductionResult:
    try:
        draft = _load_json(output_dir / "gallery-template.draft.json")
        receipt = _load_json(output_dir / "asset-receipt.json")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 正式投影源或 Asset Receipt 无法读取。",
            {},
        )
    if not isinstance(draft, dict) or not isinstance(receipt, dict):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 正式投影源或 Asset Receipt 形状无效。",
            {},
        )
    delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
    if delivery_errors:
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 恢复要求唯一且摘要一致的候选图与确认模板图谱系。",
            {"errors": delivery_errors},
        )
    expected_object_key = _object_storage_key(
        manifest["templateKey"],
        delivery["approvedPath"],
        delivery["approvedSha256"],
        rules,
    )
    if not _asset_receipt_valid(
        receipt, manifest, delivery, expected_object_key, rules
    ):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 Asset Receipt 与确认模板图或对象键不一致。",
            {"path": str(output_dir / "asset-receipt.json")},
        )

    receipt_fields = rules["objectStorageContract"]["receiptFields"]
    final_record = _formal_projection(draft, receipt[receipt_fields["url"]], rules)
    final_validation = _validate_final(final_record, rules)
    _atomic_write_new(output_dir / "final-validation-report.json", _json_bytes(final_validation))
    _record_artifact(
        manifest,
        output_dir,
        "final-validation-report.json",
        rules["productionPhases"][8]["phase"],
        ["gallery-template.draft.json", "asset-receipt.json"],
    )
    if not final_validation["pass"]:
        raise _stop(rules, "blocked", "contractFailure", "正式 JSON 最终合同验证未通过。", final_validation)
    _atomic_write_new(output_dir / "gallery-template.json", _json_bytes(final_record))
    _record_artifact(
        manifest,
        output_dir,
        "gallery-template.json",
        rules["productionPhases"][8]["phase"],
        ["gallery-template.draft.json", "asset-receipt.json", "final-validation-report.json"],
    )
    _advance(manifest, rules, rules["productionPhases"][8]["phase"], timestamp)
    manifest["outcome"] = "completed"
    _persist_manifest(output_dir, manifest)
    return ProductionResult(
        "completed",
        manifest["productionItemId"],
        rules["resultStates"]["completed"],
        output_dir,
        output_dir / "gallery-template.json",
        resumed=True,
        major_stage=rules["majorStageContract"]["stages"][3]["selector"],
        primary_artifact=output_dir / "gallery-template.json",
    )
