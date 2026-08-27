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


def _first_stage_requirement_results(
    plan: dict[str, Any],
    source_analysis: dict[str, Any],
    sections: dict[str, str],
    prompt: str,
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """Derive P1 qualification from the authored graph and frozen prompt."""

    gate_contract = rules["sourceAuthoringContextContract"][
        "firstStageGateContract"
    ]
    requirement_ids = rules["criticalOutcomeContract"]["requirementIds"]
    fields = rules["criticalOutcomeContract"]["requirementResultFields"]
    identity_contract = rules["identityReplacementContract"]
    dependency_fields = identity_contract["dependencyFields"]
    component_id_field = dependency_fields["componentIdentity"]
    closure_ids = {
        item[component_id_field]
        for item in plan.get("dependencyClosure", [])
        if isinstance(item, dict)
        and isinstance(item.get(component_id_field), str)
        and item[component_id_field].strip()
    }
    multi = rules["multiInstanceContract"]
    operation_fields = multi["operationFields"]
    operation_target_ids = {
        component_id
        for operation in plan.get(multi["planFields"]["imageOperations"], [])
        if isinstance(operation, dict)
        for component_id in operation.get(
            operation_fields["targetRegions"], []
        )
        if isinstance(component_id, str) and component_id.strip()
    }
    dependency_pass = bool(closure_ids and closure_ids == operation_target_ids)

    context = rules["sourceAuthoringContextContract"]
    binding = plan.get(context["subjectBindingField"], {})
    binding_fields = context["subjectBindingFields"]
    group_fields = context["subjectBindingGroupFields"]
    groups = (
        binding.get(binding_fields["groups"], [])
        if isinstance(binding, dict)
        else []
    )
    touched_groups = [
        group
        for group in groups
        if isinstance(group, dict)
        and set(group.get(group_fields["requiredComponents"], [])) & closure_ids
    ]
    source_category = plan["primaryTargets"][0]["sourceCategory"]
    identity_categories = {
        rules["sourceCategories"][route["sourceCategoryRole"]]
        for route in identity_contract["routes"].values()
    }
    identity_feature_pass = bool(
        source_category not in identity_categories
        or touched_groups
        and all(
            set(group[group_fields["requiredComponents"]]) <= closure_ids
            for group in touched_groups
        )
    )
    all_identity_units = {
        identity_unit
        for group in groups
        if isinstance(group, dict)
        for identity_unit in group.get(group_fields["identityUnits"], [])
    }
    all_bound_components = {
        component_id
        for group in groups
        if isinstance(group, dict)
        for component_id in group.get(group_fields["requiredComponents"], [])
    }
    multi_subject_pass = bool(
        source_category not in identity_categories
        or len(all_identity_units) <= 1
        or all_bound_components <= closure_ids
    )

    visual = source_analysis.get("visualContract", {})
    visual_fields = context["sourceVisualContractFields"]
    section_by_visual_role = {
        "medium": "styleMedium",
        "form": "styleForm",
        "edge": "styleEdge",
        "colorAndShading": "styleColorAndShading",
        "surface": "styleSurface",
        "composition": "styleComposition",
    }
    style_pass = bool(
        isinstance(visual, dict)
        and all(
            isinstance(visual.get(visual_fields[role]), str)
            and visual[visual_fields[role]].strip()
            and visual[visual_fields[role]]
            in sections.get(section_by_visual_role[role], "")
            for role in section_by_visual_role
        )
    )
    canvas_contract = rules["sourceCanvasContract"]
    canvas_fields = canvas_contract["fields"]
    canvas = source_analysis.get(canvas_contract["field"], {})
    source_canvas_section = sections.get("sourceCanvas", "")
    canvas_pass = bool(
        isinstance(canvas, dict)
        and source_canvas_section
        and all(
            value in source_canvas_section
            for role in (
                "targetRegions",
                "excludedCarrierRegions",
                "requiredActions",
                "preserveDesignFeatures",
            )
            for value in canvas.get(canvas_fields[role], [])
        )
    )
    mark_contract = rules["sourceMarkTreatmentContract"]
    policy_fields = mark_contract["fields"]
    treatment_fields = mark_contract["treatmentFields"]
    mark_policy = source_analysis.get(mark_contract["field"], {})
    treatments = (
        mark_policy.get(policy_fields["treatments"], [])
        if isinstance(mark_policy, dict)
        else []
    )
    mark_section = sections.get("sourceMarks", "")
    mark_pass = bool(
        isinstance(mark_policy, dict)
        and mark_policy.get(policy_fields["assessed"]) is True
        and mark_section
        and all(
            item[treatment_fields[role]] in mark_section
            for item in treatments
            for role in ("identity", "type", "region", "action")
        )
    )
    prompt_pass = bool(prompt.strip() and prompt == "\n".join(sections.values()))

    raw_results = (
        (
            requirement_ids["replacementDependencyClosure"],
            dependency_pass,
            f"依赖闭包 {len(closure_ids)} 个组件与图像操作目标精确对账",
        ),
        (
            requirement_ids["sourceStyleFidelity"],
            style_pass,
            "媒介、形态、边缘、色光、表面与构图六维逐项进入冻结 Prompt",
        ),
        (
            requirement_ids["identityFeatureBinding"],
            identity_feature_pass,
            f"身份替换命中 {len(touched_groups)} 个主体绑定组并覆盖其专属组件",
        ),
        (
            requirement_ids["multiSubjectClosure"],
            multi_subject_pass,
            f"主体身份单元 {len(all_identity_units)} 个，多主体时全组同步进入换图闭包",
        ),
        (
            requirement_ids["sourceCanvasNormalization"],
            canvas_pass,
            "商品载体/截图边框已在 P1 路由为明确目标画布和排除动作",
        ),
        (
            requirement_ids["sourceMarkPolicy"],
            mark_pass,
            f"{len(treatments)} 个来源标记逐项绑定保留、同步或删除策略",
        ),
        (
            requirement_ids["generationPromptFrozen"],
            prompt_pass,
            "Generation Package 字面由固定顺序段落唯一编译并绑定 SHA-256",
        ),
    )
    return [
        {
            fields["identity"]: requirement_id,
            fields["pass"]: passed,
            fields["evidence"]: evidence,
        }
        for requirement_id, passed, evidence in raw_results
    ]


def _compile_generation_package(
    plan: dict[str, Any], source_analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    target = plan["primaryTargets"][0]
    dependency_value_field = rules["identityReplacementContract"]["dependencyFields"][
        "description"
    ]
    context_contract = rules["sourceAuthoringContextContract"]
    visual_fields = context_contract["sourceVisualContractFields"]
    visual = source_analysis["visualContract"]
    sections = {
        "task": (
            "基于参考图完成局部身份重构。参考图是媒介、画风、构图和空间关系的唯一视觉事实源；"
            "只改变替换计划与主体绑定组明确授权的组件，其余画面保持不变。"
        ),
        "replacementTarget": f"将{target['sourceRole']}完整替换为{target['replacementValue']}。",
        "dependencyClosure": "；".join(
            item[dependency_value_field] for item in plan["dependencyClosure"]
        ),
    }
    binding_fields = context_contract["subjectBindingFields"]
    group_fields = context_contract["subjectBindingGroupFields"]
    binding = plan["subjectBindingAnalysis"]
    binding_lines = [
        (
            f"绑定组 {group[group_fields['identity']]}（{group[group_fields['relationship']]}）"
            f"包含身份单元 {','.join(group[group_fields['identityUnits']])}；"
            f"必须同步替换组件 {','.join(group[group_fields['requiredComponents']])}；"
            f"依据：{group[group_fields['evidence']]}"
        )
        for group in binding[binding_fields["groups"]]
        if set(group[group_fields["requiredComponents"]])
        & {
            item[rules["identityReplacementContract"]["dependencyFields"]["componentIdentity"]]
            for item in plan["dependencyClosure"]
        }
    ]
    sections["subjectBindings"] = "；".join(binding_lines) or (
        "当前替换目标不涉及身份主体绑定；按依赖闭包执行内容替换。"
    )
    canvas_contract = rules["sourceCanvasContract"]
    canvas_fields = canvas_contract["fields"]
    canvas_modes = canvas_contract["modes"]
    canvas = source_analysis[canvas_contract["field"]]
    canvas_mode = canvas[canvas_fields["mode"]]
    targets = "、".join(canvas[canvas_fields["targetRegions"]])
    excluded = "、".join(canvas[canvas_fields["excludedCarrierRegions"]]) or "无"
    actions = "、".join(canvas[canvas_fields["requiredActions"]])
    preserved = "、".join(canvas[canvas_fields["preserveDesignFeatures"]])
    if canvas_mode == canvas_modes["printArtwork"]:
        canvas_instruction = (
            "只提取衣服表面的独立印花并正视化；排除衣服本体、模特身体、"
            "衣架、商品阴影和拍摄环境，不生成 T 恤 mockup 或穿着效果"
        )
    elif canvas_mode == canvas_modes["screenContent"]:
        canvas_instruction = (
            "只保留截图内容区；裁掉黑色截屏框、设备边框、界面控件和屏幕外环境"
        )
    elif canvas_mode == canvas_modes["fullScene"]:
        canvas_instruction = "完整商品或场景本身承担玩法，保持完整场景画布"
    else:
        canvas_instruction = "来源为独立设计，保持完整来源画布"
    sections["sourceCanvas"] = (
        f"目标画布：{canvas_instruction}；目标区域 ID：{targets}；"
        f"排除载体区域 ID：{excluded}；执行动作：{actions}；"
        f"保留设计内部结构：{preserved}。"
    )
    mark_contract = rules["sourceMarkTreatmentContract"]
    policy_fields = mark_contract["fields"]
    treatment_fields = mark_contract["treatmentFields"]
    mark_policy = source_analysis[mark_contract["field"]]
    mark_lines = [
        (
            f"{item[treatment_fields['identity']]}|{item[treatment_fields['type']]}|"
            f"{item[treatment_fields['region']]}|{item[treatment_fields['action']]}："
            f"{item[treatment_fields['evidence']]}"
        )
        for item in mark_policy[policy_fields["treatments"]]
    ]
    sections["sourceMarks"] = (
        "来源标记逐项策略："
        + ("；".join(mark_lines) if mark_lines else "已核对，未发现需单独处置的标记")
        + "。普通贴纸、装饰图标和用户未要求删除的商标不视为污染。"
    )
    sections.update(
        {
            "frozenSet": "；".join(plan["frozenSet"]),
            "styleMedium": f"媒介必须保持：{visual[visual_fields['medium']]}。",
            "styleForm": f"造型与比例必须保持：{visual[visual_fields['form']]}。",
            "styleEdge": f"轮廓与边缘必须保持：{visual[visual_fields['edge']]}。",
            "styleColorAndShading": (
                f"色彩与明暗关系必须保持：{visual[visual_fields['colorAndShading']]}。"
            ),
            "styleSurface": f"纹理与材质表现必须保持：{visual[visual_fields['surface']]}。",
            "styleComposition": f"构图、裁切与占幅必须保持：{visual[visual_fields['composition']]}。",
            "residueCleanup": (
                "清理旧身份特征与旧轮廓；来源标记只按逐项策略执行，"
                "禁止自行删除已标记保留的贴纸、装饰图标或商标。"
            ),
            "spatialRelations": "；".join(source_analysis["spatialRelations"]),
        }
    )
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
    sections["output"] = "输出一张图；保持完整画布与原比例，清晰输出，不新增文字。"
    prompt = "\n".join(sections.values())
    gate_contract = context_contract["firstStageGateContract"]
    gate_fields = gate_contract["fields"]
    requirement_fields = rules["criticalOutcomeContract"][
        "requirementResultFields"
    ]
    requirement_ids = rules["criticalOutcomeContract"]["requirementIds"]
    requirement_results = _first_stage_requirement_results(
        plan, source_analysis, sections, prompt, rules
    )
    requirement_pass_by_id = {
        result[requirement_fields["identity"]]: result[requirement_fields["pass"]]
        for result in requirement_results
    }
    first_stage_gate = {
        gate_fields["dependencyClosure"]: requirement_pass_by_id[
            requirement_ids["replacementDependencyClosure"]
        ],
        gate_fields["subjectBindings"]: all(
            requirement_pass_by_id[requirement_ids[role]]
            for role in ("identityFeatureBinding", "multiSubjectClosure")
        ),
        gate_fields["visualContract"]: requirement_pass_by_id[
            requirement_ids["sourceStyleFidelity"]
        ],
        gate_fields["sourceCanvas"]: requirement_pass_by_id[
            requirement_ids["sourceCanvasNormalization"]
        ],
        gate_fields["sourceMarks"]: requirement_pass_by_id[
            requirement_ids["sourceMarkPolicy"]
        ],
        gate_fields["prompt"]: requirement_pass_by_id[
            requirement_ids["generationPromptFrozen"]
        ],
        gate_fields["promptSha256"]: _sha_bytes(prompt.encode("utf-8")),
        gate_fields["requirementResults"]: requirement_results,
    }
    if not all(
        first_stage_gate[gate_fields[role]] is True
        for role in (
            "dependencyClosure",
            "subjectBindings",
            "visualContract",
            "sourceCanvas",
            "sourceMarks",
            "prompt",
        )
    ):
        raise ValueError("first-stage gate cannot compile a complete generation prompt")
    request_id = "gen-" + _sha_bytes(
        _canonical_bytes({"plan": plan, "prompt": prompt})
    )[:24]
    multi_contract = rules["multiInstanceContract"]
    return {
        "artifactType": "generation-package",
        "schemaVersion": plan["schemaVersion"],
        "requestId": request_id,
        "prompt": prompt,
        "sections": sections,
        gate_contract["field"]: first_stage_gate,
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
    package["prompt"] = (
        package["prompt"]
        + "\n纠正要求：修复上一版本未通过的门禁："
        + "、".join(correction["failedGates"])
        + "；其余已通过约束继续保持。"
    )
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
        intent_fields["prompt"]: generation_package["prompt"],
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
    execution_contract = rules["productionExecutionContract"]
    if (
        manifest.get(execution_contract["manifestFields"]["executionMode"])
        == execution_contract["executionModes"]["liveExternal"]
        and generation_wal.get(wal_fields["provider"])
        != contract["providerRoles"]["fal"]
    ):
        errors.append("live generation provider is not Fal")
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


def current_generation_qualification_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    source_sha256: Any,
    generation_options: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    """Replay the current generation task, WAL, provider, and image facts."""

    return _current_generation_execution_errors(
        output_dir,
        manifest,
        source_sha256,
        generation_options,
        rules,
    )


def _evaluate_visual_gate(
    review: Any,
    rules: dict[str, Any],
    expected_bindings: dict[str, str],
    identity_text_required: bool,
    expected_image_operations: list[dict[str, Any]],
    expected_component_graph: dict[str, Any],
    expected_source_canvas: dict[str, Any],
    expected_source_mark_policy: dict[str, Any],
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
    interaction_evidence = review.get(evidence_fields["interactionIntegrity"])
    source_canvas_evidence = review.get(evidence_fields["sourceCanvas"])
    source_mark_evidence = review.get(evidence_fields["sourceMarkTreatments"])
    canvas_contract = rules["sourceCanvasContract"]
    canvas_fields = canvas_contract["fields"]
    canvas_evidence_fields = contract["sourceCanvasEvidenceFields"]
    mark_contract = rules["sourceMarkTreatmentContract"]
    mark_policy_fields = mark_contract["fields"]
    treatment_fields = mark_contract["treatmentFields"]
    mark_review_fields = mark_contract["reviewEvidenceFields"]
    expected_mark_treatments = {
        item[treatment_fields["identity"]]: item
        for item in expected_source_mark_policy[mark_policy_fields["treatments"]]
    }
    expected_operation_ids = {
        operation[operation_fields["identity"]] for operation in expected_image_operations
    }
    graph_fields = multi_contract["graphFields"]
    relation_fields = multi_contract["relationFields"]
    interaction_fields = contract["interactionIntegrityFields"]
    interaction_types = {
        multi_contract["relationTypes"][role]
        for role in contract["interactionRelationTypeKeys"]
    }
    expected_interactions = {
        relation[relation_fields["identity"]]: {
            interaction_fields["relationType"]: relation[
                relation_fields["type"]
            ],
            interaction_fields["endpointComponents"]: [
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            ],
        }
        for relation in expected_component_graph[graph_fields["relations"]]
        if relation[relation_fields["type"]] in interaction_types
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
        and isinstance(interaction_evidence, list)
        and len(interaction_evidence) == len(expected_interactions)
        and all(
            isinstance(item, dict)
            and set(item) == set(interaction_fields.values())
            and item.get(interaction_fields["relationIdentity"])
            in expected_interactions
            and item.get(interaction_fields["relationType"])
            == expected_interactions[
                item[interaction_fields["relationIdentity"]]
            ][interaction_fields["relationType"]]
            and item.get(interaction_fields["endpointComponents"])
            == expected_interactions[
                item[interaction_fields["relationIdentity"]]
            ][interaction_fields["endpointComponents"]]
            and all(
                isinstance(item.get(interaction_fields[field]), bool)
                for field in (
                    "subjectPartsTraceable",
                    "topologyPlausible",
                    "contactPlausible",
                    "occlusionOrderPlausible",
                    "noFusionOrExtraParts",
                )
            )
            and isinstance(item.get(interaction_fields["evidence"]), str)
            and item[interaction_fields["evidence"]].strip()
            for item in interaction_evidence
        )
        and len(
            {
                item[interaction_fields["relationIdentity"]]
                for item in interaction_evidence
            }
        )
        == len(interaction_evidence)
        and isinstance(source_canvas_evidence, dict)
        and set(source_canvas_evidence) == set(canvas_evidence_fields.values())
        and source_canvas_evidence.get(canvas_evidence_fields["mode"])
        == expected_source_canvas[canvas_fields["mode"]]
        and all(
            isinstance(source_canvas_evidence.get(canvas_evidence_fields[field]), bool)
            for field in (
                "actionsSatisfied",
                "excludedCarrierAbsent",
                "designFeaturesPreserved",
            )
        )
        and isinstance(
            source_canvas_evidence.get(canvas_evidence_fields["evidence"]), str
        )
        and source_canvas_evidence[canvas_evidence_fields["evidence"]].strip()
        and isinstance(source_mark_evidence, list)
        and len(source_mark_evidence) == len(expected_mark_treatments)
        and all(
            isinstance(item, dict)
            and set(item) == set(mark_review_fields.values())
            and item.get(mark_review_fields["identity"]) in expected_mark_treatments
            and item.get(mark_review_fields["type"])
            == expected_mark_treatments[item[mark_review_fields["identity"]]][
                treatment_fields["type"]
            ]
            and item.get(mark_review_fields["action"])
            == expected_mark_treatments[item[mark_review_fields["identity"]]][
                treatment_fields["action"]
            ]
            and isinstance(item.get(mark_review_fields["actionSatisfied"]), bool)
            and isinstance(item.get(mark_review_fields["evidence"]), str)
            and item[mark_review_fields["evidence"]].strip()
            for item in source_mark_evidence
        )
        and len(
            {
                item[mark_review_fields["identity"]]
                for item in source_mark_evidence
            }
        )
        == len(source_mark_evidence)
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
    derived_cleanliness_gates = {
        contract["hardGateRoles"]["fullCanvasCleanliness"],
    }
    failures = [
        name
        for name, passed in hard_gates.items()
        if name not in derived_cleanliness_gates and passed is not True
    ]
    failures.extend(name for name, value in dimensions.items() if value["pass"] is not True)
    failures.extend(name for name, found in cleanliness.items() if found is True)
    if any(
        source_canvas_evidence[canvas_evidence_fields[field]] is not True
        for field in (
            "actionsSatisfied",
            "excludedCarrierAbsent",
            "designFeaturesPreserved",
        )
    ):
        failures.append(contract["hardGateRoles"]["fullCanvasCleanliness"])
    remove_action = mark_contract["actions"]["remove"]
    preserve_action = mark_contract["actions"]["preserve"]
    watermark_types = {
        mark_contract["types"]["watermark"],
        mark_contract["types"]["platformMark"],
        mark_contract["types"]["accountMark"],
        mark_contract["types"]["pseudoSignature"],
    }
    for item in source_mark_evidence:
        if item[mark_review_fields["actionSatisfied"]] is True:
            continue
        action = item[mark_review_fields["action"]]
        mark_type = item[mark_review_fields["type"]]
        if action == preserve_action:
            failures.append(contract["hardGateRoles"]["nonTargetPreservation"])
        elif action == remove_action:
            failures.append(contract["hardGateRoles"]["fullCanvasCleanliness"])
            if mark_type in watermark_types:
                failures.append(contract["hardGateRoles"]["watermarkAbsence"])
        else:
            failures.append(contract["hardGateRoles"]["dependencyClosure"])
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
    for item in interaction_evidence:
        if any(
            item[interaction_fields[field]] is not True
            for field in (
                "subjectPartsTraceable",
                "topologyPlausible",
                "contactPlausible",
                "occlusionOrderPlausible",
                "noFusionOrExtraParts",
            )
        ):
            failures.append(contract["hardGateRoles"]["contactGeometry"])
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
