from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .artifacts import canonical_json_bytes


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def template_key_is_operational_only(key: str, rules: dict[str, Any]) -> bool:
    contract = rules["templateIdentityContract"]
    return any(
        re.fullmatch(pattern, key, flags=re.IGNORECASE)
        for pattern in contract["operationalOnlyKeyPatterns"]
    )


def deterministic_template_identity_resolution(
    source_image: Path,
    request: dict[str, Any],
    rules: dict[str, Any],
    *,
    registry_revision: str,
) -> dict[str, Any]:
    """Build the explicit empty-registry fixture decision used by deterministic tests."""

    contract = rules["templateIdentityContract"]
    fields = contract["fields"]
    proposed_key = request["templateKey"]
    semantic_key = not template_key_is_operational_only(proposed_key, rules)
    return {
        fields["artifactType"]: contract["artifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["sourceImageSha256"]: hashlib.sha256(
            source_image.read_bytes()
        ).hexdigest(),
        fields["proposedKey"]: proposed_key,
        fields["resolvedKey"]: proposed_key,
        fields["status"]: contract["statuses"]["new"],
        fields["registryRevision"]: registry_revision,
        fields["registryAvailable"]: True,
        fields["sourceMatch"]: False,
        fields["collisionFree"]: True,
        fields["semanticKey"]: semantic_key,
        fields["evidence"]: (
            "确定性 fixture 使用显式空注册表，已完成来源摘要查询、key 冲突查询和语义形态审核"
        ),
    }


def template_identity_resolution_errors(
    resolution: dict[str, Any],
    *,
    source_sha256: str,
    proposed_key: str,
    rules: dict[str, Any],
) -> tuple[str | None, list[str]]:
    contract = rules["templateIdentityContract"]
    fields = contract["fields"]
    statuses = contract["statuses"]
    expected_fields = set(fields.values())
    if not isinstance(resolution, dict) or set(resolution) != expected_fields:
        return "templateKeyRegistryUnavailable", ["模板身份解析证据形状无效"]
    if resolution[fields["registryAvailable"]] is not True:
        return "templateKeyRegistryUnavailable", ["模板 key 注册表不可用"]
    if not (
        isinstance(resolution[fields["registryRevision"]], str)
        and resolution[fields["registryRevision"]].strip()
        and isinstance(resolution[fields["evidence"]], str)
        and resolution[fields["evidence"]].strip()
    ):
        return "templateKeyRegistryUnavailable", ["注册表修订号或查询证据缺失"]
    if resolution[fields["sourceImageSha256"]] != source_sha256:
        return "templateKeyExistingMismatch", ["模板身份查询未绑定当前来源图摘要"]
    if resolution[fields["proposedKey"]] != proposed_key:
        return "templateKeyExistingMismatch", ["模板身份查询的 proposedKey 与请求不一致"]
    status = resolution[fields["status"]]
    if status not in statuses.values():
        return "templateKeyRegistryUnavailable", ["模板身份查询状态无效"]
    if resolution[fields["resolvedKey"]] != proposed_key:
        return "templateKeyExistingMismatch", ["请求 key 与注册表冻结 key 不一致"]
    if status == statuses["existing"]:
        if resolution[fields["sourceMatch"]] is not True:
            return "templateKeyExistingMismatch", ["已有 key 未匹配当前来源图摘要"]
    else:
        if resolution[fields["sourceMatch"]] is not False:
            return "templateKeyExistingMismatch", ["新 key 查询却声明命中已有来源"]
        if template_key_is_operational_only(proposed_key, rules):
            return "templateKeySemanticInvalid", [
                "新模板 key 只包含素材号、批次号、日期或版本号"
            ]
        if resolution[fields["semanticKey"]] is not True:
            return "templateKeySemanticInvalid", ["新模板 key 未通过语义审核"]
    if resolution[fields["collisionFree"]] is not True:
        return "templateKeyConflict", ["模板 key 与其他来源或业务身份冲突"]
    return None, []


def compile_authoring_review_request(
    analysis: dict[str, Any], approved_sha256: str, rules: dict[str, Any]
) -> dict[str, Any]:
    contract = rules["authoringContractAudit"]
    fields = contract["requestFields"]
    approved_fields = rules["multiInstanceContract"]["approvedFields"]
    runtime_fields = rules["runtimeSemanticsContract"]["fields"]
    runtime = analysis.get("runtimeSemantics", {})
    return {
        fields["approvedImageSha256"]: approved_sha256,
        fields["promptTemplate"]: copy.deepcopy(analysis.get("promptTemplate")),
        fields["freeEditableContent"]: copy.deepcopy(
            analysis.get("freeEditableContent")
        ),
        fields["slotCandidates"]: copy.deepcopy(analysis.get("slotCandidates")),
        fields["componentGraph"]: copy.deepcopy(
            analysis.get(approved_fields["componentGraph"])
        ),
        fields["visualContract"]: copy.deepcopy(
            runtime.get(runtime_fields["visualContract"])
        ),
    }


def _production_prompt_clauses(prompt: str, rules: dict[str, Any]) -> list[str]:
    patterns = [
        re.compile(pattern, flags=re.IGNORECASE)
        for pattern in rules["authoringContractAudit"]["productionClausePatterns"]
    ]
    clauses = [
        clause.strip()
        for clause in re.split(r"(?<=[。！？!?;])|\n+", prompt)
        if clause.strip()
    ]
    return [clause for clause in clauses if any(pattern.search(clause) for pattern in patterns)]


def deterministic_authoring_contract_audit(
    approved_image: Path,
    review_request: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["authoringContractAudit"]
    request_fields = contract["requestFields"]
    review_fields = contract["reviewFields"]
    prompt_fields = contract["promptReviewFields"]
    slot_fields = contract["slotReviewFields"]
    prompt = review_request[request_fields["promptTemplate"]]
    leaked = _production_prompt_clauses(prompt if isinstance(prompt, str) else "", rules)
    graph = review_request[request_fields["componentGraph"]]
    graph_fields = rules["multiInstanceContract"]["graphFields"]
    component_fields = rules["multiInstanceContract"]["componentFields"]
    components = graph[graph_fields["components"]] if isinstance(graph, dict) else []
    slot_reviews = []
    for slot in review_request[request_fields["slotCandidates"]] or []:
        slot_id = slot.get("id")
        component_ids = sorted(
            component[component_fields["identity"]]
            for component in components
            if component.get(component_fields["control"]) == slot_id
        )
        suggestions = slot.get("suggestions")
        user_motivation = bool(
            isinstance(suggestions, list)
            and len(suggestions) >= 2
            and len({slot.get("defaultValue"), *suggestions}) >= 3
        )
        visually_visible = bool(component_ids)
        model_controllable = bool(
            component_ids
            and isinstance(slot.get("defaultValue"), str)
            and slot["defaultValue"].strip()
        )
        mechanism_preserved = bool(
            component_ids
            and isinstance(slot.get("semanticRole"), str)
            and slot["semanticRole"].strip()
        )
        slot_reviews.append(
            {
                slot_fields["slotIdentity"]: slot_id,
                slot_fields["componentIdentities"]: component_ids,
                slot_fields["userMotivation"]: user_motivation,
                slot_fields["visuallyVisible"]: visually_visible,
                slot_fields["modelControllable"]: model_controllable,
                slot_fields["mechanismPreserved"]: mechanism_preserved,
                slot_fields["evidence"]: (
                    f"独立复核 {slot_id} 的可见组件 {component_ids}、默认值、推荐差异和机制边界"
                ),
            }
        )
    gate_roles = (
        "userMotivation",
        "visuallyVisible",
        "modelControllable",
        "mechanismPreserved",
    )
    passed = not leaked and all(
        all(review[slot_fields[role]] is True for role in gate_roles)
        for review in slot_reviews
    )
    return {
        review_fields["artifactType"]: contract["artifactType"],
        review_fields["schemaVersion"]: rules["schemaVersion"],
        review_fields["approvedImageSha256"]: hashlib.sha256(
            approved_image.read_bytes()
        ).hexdigest(),
        review_fields["reviewRequestSha256"]: _sha256(review_request),
        review_fields["promptReview"]: {
            prompt_fields["userEditableOnly"]: not leaked,
            prompt_fields["leakedProductionClauses"]: leaked,
            prompt_fields["evidence"]: (
                "逐句分类 Prompt 内容为用户可替换画面内容或生产/输出约束"
            ),
        },
        review_fields["slotReviews"]: slot_reviews,
        review_fields["pass"]: passed,
        review_fields["evidence"]: (
            "独立绑定 Approved Image、组件图、槽位候选与 Prompt 职责的作者合同审计"
        ),
    }


def authoring_contract_audit_errors(
    audit: dict[str, Any],
    review_request: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    contract = rules["authoringContractAudit"]
    request_fields = contract["requestFields"]
    review_fields = contract["reviewFields"]
    prompt_fields = contract["promptReviewFields"]
    slot_fields = contract["slotReviewFields"]
    errors: list[str] = []
    if not isinstance(audit, dict) or set(audit) != set(review_fields.values()):
        return ["作者合同审计形状无效"]
    if audit[review_fields["artifactType"]] != contract["artifactType"]:
        errors.append("作者合同审计类型无效")
    if audit[review_fields["schemaVersion"]] != rules["schemaVersion"]:
        errors.append("作者合同审计版本无效")
    if audit[review_fields["approvedImageSha256"]] != review_request[
        request_fields["approvedImageSha256"]
    ]:
        errors.append("作者合同审计未绑定当前 Approved Image")
    if audit[review_fields["reviewRequestSha256"]] != _sha256(review_request):
        errors.append("作者合同审计未绑定当前只读请求")
    prompt_review = audit[review_fields["promptReview"]]
    if not (
        isinstance(prompt_review, dict)
        and set(prompt_review) == set(prompt_fields.values())
        and prompt_review[prompt_fields["userEditableOnly"]] is True
        and prompt_review[prompt_fields["leakedProductionClauses"]] == []
        and isinstance(prompt_review[prompt_fields["evidence"]], str)
        and prompt_review[prompt_fields["evidence"]].strip()
    ):
        errors.append("promptTemplate 含生产、清理、输出或商品展示约束")
    graph = review_request[request_fields["componentGraph"]]
    graph_fields = rules["multiInstanceContract"]["graphFields"]
    component_fields = rules["multiInstanceContract"]["componentFields"]
    components = graph[graph_fields["components"]] if isinstance(graph, dict) else []
    candidates = review_request[request_fields["slotCandidates"]]
    expected_ids = [slot.get("id") for slot in candidates] if isinstance(candidates, list) else []
    reviews = audit[review_fields["slotReviews"]]
    if not isinstance(reviews, list):
        errors.append("独立槽位复核缺失")
        return errors
    review_by_id = {
        review.get(slot_fields["slotIdentity"]): review
        for review in reviews
        if isinstance(review, dict)
    }
    if len(review_by_id) != len(reviews) or set(review_by_id) != set(expected_ids):
        errors.append("独立槽位复核未完整覆盖当前候选")
    gate_roles = (
        "userMotivation",
        "visuallyVisible",
        "modelControllable",
        "mechanismPreserved",
    )
    for slot_id in expected_ids:
        review = review_by_id.get(slot_id)
        expected_components = sorted(
            component[component_fields["identity"]]
            for component in components
            if component.get(component_fields["control"]) == slot_id
        )
        if not (
            isinstance(review, dict)
            and set(review) == set(slot_fields.values())
            and review[slot_fields["componentIdentities"]] == expected_components
            and all(
                isinstance(review[slot_fields[role]], bool) for role in gate_roles
            )
            and isinstance(review[slot_fields["evidence"]], str)
            and review[slot_fields["evidence"]].strip()
        ):
            errors.append(f"槽位 {slot_id} 的独立复核绑定或证据无效")
            continue
        failed = [
            role for role in gate_roles if review[slot_fields[role]] is not True
        ]
        if failed:
            errors.append(f"槽位 {slot_id} 未通过独立价值门禁：{','.join(failed)}")
    if audit[review_fields["pass"]] is not (not errors):
        errors.append("作者合同审计 pass 与逐项结果不一致")
    if not (
        isinstance(audit[review_fields["evidence"]], str)
        and audit[review_fields["evidence"]].strip()
    ):
        errors.append("作者合同审计总证据缺失")
    return errors

