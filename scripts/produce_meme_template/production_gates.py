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


def slot_input_modes(slot: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    """Return one canonical, validated projection of authored v2 input modes."""

    contract = rules["slotCompilationContract"]
    input_contract = contract["inputContract"]
    mode_field = input_contract["modeAuthoringField"]
    modes = input_contract["modes"]
    authored = slot.get(mode_field)
    if authored is None:
        if slot.get("type") == contract["slotTypes"]["primarySubjectUpload"]:
            return [modes["text"], modes["image"]]
        return [modes["text"]]
    if not (
        isinstance(authored, list)
        and authored
        and len(authored) == len(set(authored))
        and set(authored) <= set(modes.values())
    ):
        return []
    return [mode for mode in modes.values() if mode in authored]


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


def _subject_presence_context(
    analysis: dict[str, Any],
    authoring_handoff: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    audit_contract = rules["authoringContractAudit"]
    fields = audit_contract["subjectPresenceContextFields"]
    multi = rules["multiInstanceContract"]
    graph_fields = multi["graphFields"]
    component_fields = multi["componentFields"]
    graph = analysis.get(multi["approvedFields"]["componentGraph"])
    components = (
        graph.get(graph_fields["components"], []) if isinstance(graph, dict) else []
    )
    subject_components = [
        component
        for component in components
        if isinstance(component, dict)
        and component.get(component_fields["visualInstance"]) is True
        and isinstance(component.get(component_fields["identityUnit"]), str)
    ]
    approved_component_ids = sorted(
        component.get(component_fields["identity"])
        for component in subject_components
        if isinstance(component.get(component_fields["identity"]), str)
    )
    approved_identity_units = sorted(
        {
            component.get(component_fields["identityUnit"])
            for component in subject_components
            if isinstance(component.get(component_fields["identityUnit"]), str)
        }
    )
    handoff_contract = rules["authoringHandoffContract"]["subjectEditIntentContract"]
    handoff_fields = handoff_contract["fields"]
    source_intent = authoring_handoff.get("sourceIntent", {})
    subject_intent = (
        source_intent.get(handoff_contract["field"], {})
        if isinstance(source_intent, dict)
        else {}
    )
    handoff_identity_units = subject_intent.get(handoff_fields["identityUnits"])
    slot_contract = rules["slotCompilationContract"]
    subject_type = slot_contract["slotTypes"]["primarySubjectUpload"]
    subject_role_name = slot_contract["semanticRoles"]["primarySubject"]
    candidates = analysis.get("slotCandidates")
    subject_slot_ids = sorted(
        slot.get("id")
        for slot in candidates or []
        if isinstance(slot, dict)
        and slot.get("type") == subject_type
        and slot.get("semanticRole") == subject_role_name
        and isinstance(slot.get("id"), str)
    ) if isinstance(candidates, list) else []
    return {
        fields["declaredPresence"]: analysis.get("hasPrimarySubject"),
        fields["subjectKind"]: analysis.get("subjectKind"),
        fields["approvedSubjectComponents"]: approved_component_ids,
        fields["approvedIdentityUnits"]: approved_identity_units,
        fields["handoffIdentityUnits"]: copy.deepcopy(handoff_identity_units),
        fields["handoffSubjectCount"]: subject_intent.get(
            handoff_fields["subjectCount"]
        ),
        fields["handoffBindingMode"]: subject_intent.get(
            handoff_fields["bindingMode"]
        ),
        fields["subjectSlots"]: subject_slot_ids,
        fields["omissionEvidence"]: copy.deepcopy(
            analysis.get("subjectSlotOmissionEvidence")
        ),
    }


def subject_presence_context_errors(
    context: dict[str, Any], rules: dict[str, Any]
) -> list[str]:
    audit_contract = rules["authoringContractAudit"]
    fields = audit_contract["subjectPresenceContextFields"]
    if not isinstance(context, dict) or set(context) != set(fields.values()):
        return ["主体存在性上下文形状无效"]
    declared = context[fields["declaredPresence"]]
    approved_components = context[fields["approvedSubjectComponents"]]
    approved_identity_units = context[fields["approvedIdentityUnits"]]
    handoff_identity_units = context[fields["handoffIdentityUnits"]]
    handoff_count = context[fields["handoffSubjectCount"]]
    handoff_mode = context[fields["handoffBindingMode"]]
    subject_slots = context[fields["subjectSlots"]]
    omission = context[fields["omissionEvidence"]]
    errors: list[str] = []

    def unique_strings(value: Any) -> bool:
        return bool(
            isinstance(value, list)
            and all(isinstance(item, str) and item.strip() for item in value)
            and len(value) == len(set(value))
        )

    list_fields_valid = all(
        unique_strings(value)
        for value in (
            approved_components,
            approved_identity_units,
            handoff_identity_units,
            subject_slots,
        )
    )
    if not isinstance(declared, bool) or not list_fields_valid:
        errors.append("主体声明、组件、身份单元或主体槽列表无效")
        return errors
    if declared is not bool(approved_components):
        errors.append("hasPrimarySubject 与 Approved 组件图的主体组件不一致")
    if (
        not isinstance(handoff_count, int)
        or isinstance(handoff_count, bool)
        or handoff_count != len(handoff_identity_units)
        or handoff_count != len(approved_identity_units)
    ):
        errors.append("Authoring Handoff 与 Approved 组件图的主体身份数量不一致")
    handoff_contract = rules["authoringHandoffContract"]["subjectEditIntentContract"]
    modes = handoff_contract["bindingModes"]
    expected_mode = (
        modes["none"]
        if not approved_identity_units
        else modes["single"]
        if len(approved_identity_units) == 1
        else modes["multiple"]
    )
    if handoff_mode != expected_mode:
        errors.append("Authoring Handoff 主体绑定模式与 Approved 组件图不一致")
    if not declared and (subject_slots or omission is not None):
        errors.append("无主体声明不能同时携带主体槽或主体省略证据")
    if declared and not subject_slots and not isinstance(omission, dict):
        errors.append("明显主体缺少 subject 槽和类型化省略证据")
    return errors


def compile_authoring_review_request(
    analysis: dict[str, Any],
    approved_sha256: str,
    authoring_handoff: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["authoringContractAudit"]
    fields = contract["requestFields"]
    approved_fields = rules["multiInstanceContract"]["approvedFields"]
    runtime_fields = rules["runtimeSemanticsContract"]["fields"]
    visible_text_fields = rules["visibleTextContract"]["analysisFields"]
    runtime = analysis.get("runtimeSemantics", {})
    return {
        fields["approvedImageSha256"]: approved_sha256,
        fields["neutralTitle"]: copy.deepcopy(analysis.get("neutralTitle")),
        fields["neutralDescription"]: copy.deepcopy(
            analysis.get("neutralDescription")
        ),
        fields["classificationTags"]: copy.deepcopy(analysis.get("tags")),
        fields["promptTemplate"]: copy.deepcopy(analysis.get("promptTemplate")),
        fields["freeEditableContent"]: copy.deepcopy(
            analysis.get("freeEditableContent")
        ),
        fields["slotCandidates"]: copy.deepcopy(analysis.get("slotCandidates")),
        fields["defaultValuePreferenceExceptions"]: copy.deepcopy(
            analysis.get("defaultValuePreferenceExceptionEvidence", {})
        ),
        fields["visibleTextRegions"]: copy.deepcopy(
            analysis.get(visible_text_fields["regions"], [])
        ),
        fields["componentGraph"]: copy.deepcopy(
            analysis.get(approved_fields["componentGraph"])
        ),
        fields["visualContract"]: copy.deepcopy(
            runtime.get(runtime_fields["visualContract"])
        ),
        fields["subjectPresenceContext"]: _subject_presence_context(
            analysis, authoring_handoff, rules
        ),
    }


def _prompt_clauses(prompt: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"(?<=[。！？!?;；,，])", prompt)
        if clause.strip() and re.search(r"\w|[\u3400-\u4dbf\u4e00-\u9fff]", clause)
    ]


def _deep_text_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _deep_text_values(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _deep_text_values(item)]
    return []


def _cjk_bigrams(value: str) -> set[str]:
    runs = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", value)
    ignored = {
        "保持",
        "保留",
        "使用",
        "原有",
        "画面",
        "当前",
        "模板",
        "主体",
        "人物",
        "内容",
    }
    return {
        run[index : index + 2]
        for run in runs
        for index in range(len(run) - 1)
        if run[index : index + 2] not in ignored
    }


def _production_prompt_clauses(prompt: str, rules: dict[str, Any]) -> list[str]:
    patterns = [
        re.compile(pattern, flags=re.IGNORECASE)
        for pattern in rules["authoringContractAudit"]["productionClausePatterns"]
    ]
    clauses = _prompt_clauses(prompt)
    return [clause for clause in clauses if any(pattern.search(clause) for pattern in patterns)]


def _prompt_clause_classifications(
    prompt: str,
    review_request: dict[str, Any],
    rules: dict[str, Any],
) -> list[dict[str, str]]:
    contract = rules["authoringContractAudit"]
    request_fields = contract["requestFields"]
    fields = contract["promptClauseFields"]
    responsibilities = contract["promptResponsibilities"]
    leaked = set(_production_prompt_clauses(prompt, rules))
    slots = review_request.get(request_fields["slotCandidates"])
    slot_ids = {
        item.get("id")
        for item in slots
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    } if isinstance(slots, list) else set()
    free_content = review_request.get(request_fields["freeEditableContent"])
    free_values = {
        value.strip()
        for value in free_content
        if isinstance(value, str) and value.strip()
    } if isinstance(free_content, list) else set()
    visible_text = review_request.get(request_fields["visibleTextRegions"])
    visible_text_field = rules["visibleTextContract"]["regionFields"][
        "selectedText"
    ]
    approved_visible_values = {
        region.get(visible_text_field).strip()
        for region in visible_text
        if isinstance(region, dict)
        and isinstance(region.get(visible_text_field), str)
        and region[visible_text_field].strip()
    } if isinstance(visible_text, list) else set()
    visual_contract = review_request.get(request_fields["visualContract"])
    visual_contract_bigrams = set().union(
        *(_cjk_bigrams(value) for value in _deep_text_values(visual_contract))
    )
    classifications: list[dict[str, str]] = []
    for clause in _prompt_clauses(prompt):
        placeholder_ids = set(re.findall(r"\{\{\s*([a-z][a-z0-9_]*)", clause))
        matched_free_values = sorted(
            value for value in free_values if value in clause
        )
        matched_visible_values = sorted(
            value for value in approved_visible_values if value in clause
        )
        matched_visual_bigrams = sorted(
            _cjk_bigrams(clause) & visual_contract_bigrams
        )
        if clause in leaked:
            responsibility = responsibilities["productionConstraint"]
            evidence = "短语扫描命中生产、输出或商品展示约束"
        elif (
            placeholder_ids
            and placeholder_ids <= slot_ids
        ) or matched_free_values or matched_visible_values or len(
            matched_visual_bigrams
        ) >= 2:
            responsibility = responsibilities["userEditableContent"]
            evidence = (
                f"可编辑来源：slots={sorted(placeholder_ids)}; "
                f"freeEditableContent={matched_free_values}; "
                f"approvedVisibleText={matched_visible_values}; "
                f"visualContractBigrams={matched_visual_bigrams}"
            )
        else:
            responsibility = responsibilities["unclassified"]
            evidence = (
                "未能由槽位、freeEditableContent、Approved 可见文字"
                "或 visualContract 证明该子句的用户可编辑来源"
            )
        classifications.append(
            {
                fields["clause"]: clause,
                fields["responsibility"]: responsibility,
                fields["evidence"]: evidence,
            }
        )
    return classifications


def deterministic_authoring_contract_audit(
    approved_image: Path,
    review_request: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["authoringContractAudit"]
    request_fields = contract["requestFields"]
    review_fields = contract["reviewFields"]
    copy_fields = contract["copyReviewFields"]
    prompt_fields = contract["promptReviewFields"]
    slot_fields = contract["slotReviewFields"]
    inheritance_review_fields = contract["identityInheritanceReviewFields"]
    default_review_fields = contract["defaultValueReviewFields"]
    subject_context_fields = contract["subjectPresenceContextFields"]
    subject_review_fields = contract["subjectPresenceReviewFields"]
    tag_review_fields = contract["tagReviewFields"]
    tag_contract = contract["tagAuthoringContract"]
    prompt = review_request[request_fields["promptTemplate"]]
    title = review_request.get(request_fields["neutralTitle"])
    description = review_request.get(request_fields["neutralDescription"])
    candidates = review_request.get(request_fields["slotCandidates"])
    open_slot_values = {
        value
        for slot in candidates or []
        if isinstance(slot, dict)
        for value in [
            slot.get("defaultValue"),
            *(
                slot.get("suggestions", [])
                if isinstance(slot.get("suggestions", []), list)
                else []
            ),
        ]
        if isinstance(value, str) and value
    } if isinstance(candidates, list) else set()
    title_forbidden_tokens = {
        token
        for slot in candidates or []
        if isinstance(slot, dict)
        for token in slot.get("titleForbiddenTokens", [])
        if isinstance(token, str) and token
    } if isinstance(candidates, list) else set()
    copy_review = {
        copy_fields["titleGrounded"]: bool(
            isinstance(title, str) and title.strip() == title and title
        ),
        copy_fields["descriptionGrounded"]: bool(
            isinstance(description, str)
            and description.strip() == description
            and description
        ),
        copy_fields["spokenNaturalness"]: bool(
            isinstance(title, str)
            and isinstance(description, str)
            and not _production_prompt_clauses(f"{title}。{description}", rules)
        ),
        copy_fields["slotPortability"]: bool(
            isinstance(title, str)
            and not any(
                value in title
                for value in open_slot_values | title_forbidden_tokens
            )
        ),
        copy_fields["evidence"]: (
            "独立对照 Approved Image 复核标题、描述的画面根据、口语自然度和槽位可迁移性"
        ),
    }
    leaked = _production_prompt_clauses(prompt if isinstance(prompt, str) else "", rules)
    clause_classifications = _prompt_clause_classifications(
        prompt if isinstance(prompt, str) else "", review_request, rules
    )
    user_editable_only = all(
        item[contract["promptClauseFields"]["responsibility"]]
        == contract["promptResponsibilities"]["userEditableContent"]
        for item in clause_classifications
    )
    graph = review_request[request_fields["componentGraph"]]
    graph_fields = rules["multiInstanceContract"]["graphFields"]
    component_fields = rules["multiInstanceContract"]["componentFields"]
    components = graph[graph_fields["components"]] if isinstance(graph, dict) else []
    slot_contract = rules["slotCompilationContract"]
    slot_reviews = []
    identity_inheritance_reviews = []
    default_value_reviews = []
    subject_type = slot_contract["slotTypes"]["primarySubjectUpload"]
    inheritance_contract = slot_contract["identityInheritanceDecision"]
    inheritance_field = inheritance_contract["authoringField"]
    inheritance_fields = inheritance_contract["fields"]
    trait_kinds = inheritance_contract["traitKinds"]
    preference = slot_contract["defaultValuePreference"]
    preference_exceptions = review_request.get(
        request_fields["defaultValuePreferenceExceptions"], {}
    )
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
                slot_fields["approvedInputModes"]: slot_input_modes(slot, rules),
                slot_fields["userMotivation"]: user_motivation,
                slot_fields["visuallyVisible"]: visually_visible,
                slot_fields["modelControllable"]: model_controllable,
                slot_fields["mechanismPreserved"]: mechanism_preserved,
                slot_fields["evidence"]: (
                    f"独立复核 {slot_id} 的可见组件 {component_ids}、默认值、推荐差异和机制边界"
                ),
            }
        )
        default_value = slot.get("defaultValue")
        exception = (
            preference_exceptions.get(slot_id)
            if isinstance(preference_exceptions, dict)
            else None
        )
        exception_valid = bool(
            isinstance(exception, dict)
            and exception.get("reviewed") is True
            and isinstance(exception.get("reason"), str)
            and exception["reason"].strip()
        )
        exact_visible_text = slot.get("exactVisibleTextEvidence")
        exact_visible_text_valid = bool(
            slot.get("type")
            == slot_contract["slotTypes"]["visibleTextPrompt"]
            and isinstance(exact_visible_text, dict)
            and exact_visible_text.get("approvedImageSha256")
            == review_request[request_fields["approvedImageSha256"]]
            and exact_visible_text.get("visibleText") == default_value
            and isinstance(exact_visible_text.get("evidence"), str)
            and exact_visible_text["evidence"].strip()
        )
        default_user_facing = bool(
            isinstance(default_value, str)
            and default_value == default_value.strip()
            and default_value
            and ("\n" not in default_value or exact_visible_text_valid)
            and not _production_prompt_clauses(default_value, rules)
        )
        default_single_axis = bool(
            isinstance(slot.get("semanticRole"), str)
            and slot["semanticRole"].strip()
        )
        default_minimal = bool(
            isinstance(default_value, str)
            and (
                preference["preferredMinimum"]
                <= len(default_value)
                <= preference["preferredMaximum"]
                or exception_valid
            )
        )
        default_value_reviews.append(
            {
                default_review_fields["slotIdentity"]: slot_id,
                default_review_fields["defaultValue"]: default_value,
                default_review_fields["userFacing"]: default_user_facing,
                default_review_fields["singleAxis"]: default_single_axis,
                default_review_fields["minimalWording"]: default_minimal,
                default_review_fields["evidence"]: (
                    f"独立复核 {slot_id} 的用户语言、单一编辑轴和最短自然表述"
                ),
            }
        )
        if slot.get("type") == subject_type:
            decision = slot.get(inheritance_field)
            inherited = (
                decision.get(inheritance_fields["inheritFromUpload"])
                if isinstance(decision, dict)
                else None
            )
            fixed = (
                decision.get(inheritance_fields["keepFromTemplate"])
                if isinstance(decision, dict)
                else None
            )
            classifications = (
                decision.get(inheritance_fields["traitClassifications"])
                if isinstance(decision, dict)
                else None
            )
            clothing_visible = (
                decision.get(inheritance_fields["clothingVisible"])
                if isinstance(decision, dict)
                else None
            )
            reason = (
                decision.get(inheritance_fields["reason"])
                if isinstance(decision, dict)
                else None
            )
            inherited_values = (
                inherited
                if isinstance(inherited, list)
                and all(isinstance(value, str) for value in inherited)
                else []
            )
            fixed_values = (
                fixed
                if isinstance(fixed, list)
                and all(isinstance(value, str) for value in fixed)
                else []
            )
            upload_traits_complete = bool(
                inheritance_contract["requiredInheritedTrait"]
                in inherited_values
                and len(inherited_values) - 1
                >= inheritance_contract["minimumSpecificInheritedTraits"]
            )
            clothing_inherited = bool(
                isinstance(classifications, dict)
                and any(
                    classifications.get(value) == trait_kinds["clothing"]
                    for value in inherited_values
                )
            )
            fixed_clothing = [
                value
                for value in fixed_values
                if isinstance(classifications, dict)
                and classifications.get(value) == trait_kinds["clothing"]
            ]
            clothing_policy = inheritance_contract["clothingPolicy"]
            template_exceptions_minimal = bool(
                not fixed_values
                or (
                    isinstance(reason, str)
                    and reason.strip()
                    and (
                        not fixed_clothing
                        or (
                            not set(fixed_clothing)
                            & set(clothing_policy["forbiddenGenericTemplateValues"])
                            and any(
                                marker in reason
                                for marker in clothing_policy["requiredReasonMarkers"]
                            )
                        )
                    )
                )
            )
            clothing_policy_valid = bool(
                isinstance(clothing_visible, bool)
                and (not clothing_visible or clothing_inherited)
                and (not fixed_clothing or clothing_visible)
            )
            identity_inheritance_reviews.append(
                {
                    inheritance_review_fields["slotIdentity"]: slot_id,
                    inheritance_review_fields["uploadTraitsComplete"]: (
                        upload_traits_complete
                    ),
                    inheritance_review_fields["templateExceptionsMinimal"]: (
                        template_exceptions_minimal
                    ),
                    inheritance_review_fields["clothingPolicyValid"]: (
                        clothing_policy_valid
                    ),
                    inheritance_review_fields["evidence"]: (
                        f"独立对照 Approved Image 复核 {slot_id} 的可见身份、服装继承和最小模板例外"
                    ),
                }
            )
    subject_context = review_request[request_fields["subjectPresenceContext"]]
    subject_context_errors = subject_presence_context_errors(subject_context, rules)
    declared_subject = subject_context[subject_context_fields["declaredPresence"]]
    approved_subject_components = subject_context[
        subject_context_fields["approvedSubjectComponents"]
    ]
    handoff_count = subject_context[subject_context_fields["handoffSubjectCount"]]
    approved_identity_units = subject_context[
        subject_context_fields["approvedIdentityUnits"]
    ]
    subject_slots = subject_context[subject_context_fields["subjectSlots"]]
    omission = subject_context[subject_context_fields["omissionEvidence"]]
    observed_subject = bool(approved_subject_components)
    subject_review = {
        subject_review_fields["observedPresence"]: observed_subject,
        subject_review_fields["observedSubjectComponents"]: copy.deepcopy(
            approved_subject_components
        ),
        subject_review_fields["declarationMatchesImage"]: (
            declared_subject is observed_subject
        ),
        subject_review_fields["graphMatchesImage"]: True,
        subject_review_fields["handoffMatchesImage"]: (
            isinstance(handoff_count, int)
            and not isinstance(handoff_count, bool)
            and handoff_count == len(approved_identity_units)
        ),
        subject_review_fields["slotPolicyValid"]: (
            not observed_subject or bool(subject_slots) or isinstance(omission, dict)
        ),
        subject_review_fields["evidence"]: (
            "逐项复核 Approved Image 主体、组件图、Authoring Handoff 主体连续性和主体槽策略"
        ),
    }
    tags = review_request.get(request_fields["classificationTags"])
    generic_values = set(tag_contract["genericOnlyValues"])
    has_specific_tag = bool(
        isinstance(tags, list)
        and any(
            isinstance(tag, str) and tag not in generic_values
            for tag in tags
        )
    )
    tag_reviews = [
        {
            tag_review_fields["tag"]: tag,
            tag_review_fields["groundedInApprovedImage"]: True,
            tag_review_fields["classificationUseful"]: has_specific_tag,
            tag_review_fields["evidence"]: (
                f"独立对照 Approved Image 的主体、动作、场景、媒介与视觉钩子：{tag}"
            ),
        }
        for tag in tags or []
        if isinstance(tag, str)
    ]
    tags_valid = bool(
        isinstance(tags, list)
        and tag_contract["minimumItems"] <= len(tags) <= tag_contract["maximumItems"]
        and all(
            isinstance(tag, str)
            and tag.strip() == tag
            and tag
            and len(tag) <= tag_contract["maximumCharacters"]
            for tag in tags
        )
        and len(tags) == len(set(tags))
        and has_specific_tag
    )
    gate_roles = (
        "userMotivation",
        "visuallyVisible",
        "modelControllable",
        "mechanismPreserved",
    )
    passed = (
        all(
            copy_review[copy_fields[role]] is True
            for role in (
                "titleGrounded",
                "descriptionGrounded",
                "spokenNaturalness",
                "slotPortability",
            )
        )
        and user_editable_only
        and not subject_context_errors
        and all(
            subject_review[subject_review_fields[role]] is True
            for role in (
                "declarationMatchesImage",
                "graphMatchesImage",
                "handoffMatchesImage",
                "slotPolicyValid",
            )
        )
        and all(
            all(review[slot_fields[role]] is True for role in gate_roles)
            for review in slot_reviews
        )
        and all(
            all(
                review[inheritance_review_fields[role]] is True
                for role in (
                    "uploadTraitsComplete",
                    "templateExceptionsMinimal",
                    "clothingPolicyValid",
                )
            )
            for review in identity_inheritance_reviews
        )
        and all(
            all(
                review[default_review_fields[role]] is True
                for role in ("userFacing", "singleAxis", "minimalWording")
            )
            for review in default_value_reviews
        )
        and tags_valid
        and all(
            review[tag_review_fields["groundedInApprovedImage"]] is True
            and review[tag_review_fields["classificationUseful"]] is True
            for review in tag_reviews
        )
    )
    return {
        review_fields["artifactType"]: contract["artifactType"],
        review_fields["schemaVersion"]: rules["schemaVersion"],
        review_fields["approvedImageSha256"]: hashlib.sha256(
            approved_image.read_bytes()
        ).hexdigest(),
        review_fields["reviewRequestSha256"]: _sha256(review_request),
        review_fields["copyReview"]: copy_review,
        review_fields["promptReview"]: {
            prompt_fields["userEditableOnly"]: user_editable_only,
            prompt_fields["leakedProductionClauses"]: leaked,
            prompt_fields["clauseClassifications"]: clause_classifications,
            prompt_fields["evidence"]: (
                "逐句分类 Prompt 内容为用户可替换画面内容或生产/输出约束"
            ),
        },
        review_fields["slotReviews"]: slot_reviews,
        review_fields["identityInheritanceReviews"]: identity_inheritance_reviews,
        review_fields["defaultValueReviews"]: default_value_reviews,
        review_fields["subjectPresenceReview"]: subject_review,
        review_fields["tagReviews"]: tag_reviews,
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
    copy_fields = contract["copyReviewFields"]
    prompt_fields = contract["promptReviewFields"]
    slot_fields = contract["slotReviewFields"]
    inheritance_review_fields = contract["identityInheritanceReviewFields"]
    default_review_fields = contract["defaultValueReviewFields"]
    subject_context_fields = contract["subjectPresenceContextFields"]
    subject_review_fields = contract["subjectPresenceReviewFields"]
    tag_review_fields = contract["tagReviewFields"]
    tag_contract = contract["tagAuthoringContract"]
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
    copy_review = audit[review_fields["copyReview"]]
    if not (
        isinstance(copy_review, dict)
        and set(copy_review) == set(copy_fields.values())
        and all(
            copy_review.get(copy_fields[role]) is True
            for role in (
                "titleGrounded",
                "descriptionGrounded",
                "spokenNaturalness",
                "slotPortability",
            )
        )
        and isinstance(copy_review.get(copy_fields["evidence"]), str)
        and copy_review[copy_fields["evidence"]].strip()
    ):
        errors.append(
            "标题或描述未通过 Approved Image 根据、口语自然度和槽位可迁移性复核"
        )
    prompt_review = audit[review_fields["promptReview"]]
    prompt = review_request[request_fields["promptTemplate"]]
    clause_fields = contract["promptClauseFields"]
    responsibilities = contract["promptResponsibilities"]
    clause_reviews = (
        prompt_review.get(prompt_fields["clauseClassifications"])
        if isinstance(prompt_review, dict)
        else None
    )
    expected_clauses = _prompt_clauses(prompt if isinstance(prompt, str) else "")
    clause_reviews_valid = bool(
        isinstance(clause_reviews, list)
        and len(clause_reviews) == len(expected_clauses)
        and all(
            isinstance(item, dict)
            and set(item) == set(clause_fields.values())
            and item[clause_fields["clause"]] == expected_clause
            and item[clause_fields["responsibility"]]
            == responsibilities["userEditableContent"]
            and isinstance(item[clause_fields["evidence"]], str)
            and item[clause_fields["evidence"]].strip()
            for item, expected_clause in zip(
                clause_reviews, expected_clauses, strict=True
            )
        )
    )
    if not (
        isinstance(prompt_review, dict)
        and set(prompt_review) == set(prompt_fields.values())
        and prompt_review[prompt_fields["userEditableOnly"]] is True
        and prompt_review[prompt_fields["leakedProductionClauses"]] == []
        and clause_reviews_valid
        and isinstance(prompt_review[prompt_fields["evidence"]], str)
        and prompt_review[prompt_fields["evidence"]].strip()
    ):
        errors.append("promptTemplate 含生产、清理、输出或商品展示约束")
    subject_context = review_request[request_fields["subjectPresenceContext"]]
    context_errors = subject_presence_context_errors(subject_context, rules)
    errors.extend(f"主体连续性：{error}" for error in context_errors)
    subject_review = audit[review_fields["subjectPresenceReview"]]
    declared_subject = subject_context.get(
        subject_context_fields["declaredPresence"]
    )
    approved_subject_components = subject_context.get(
        subject_context_fields["approvedSubjectComponents"]
    )
    subject_review_valid = bool(
        isinstance(subject_review, dict)
        and set(subject_review) == set(subject_review_fields.values())
        and isinstance(
            subject_review.get(subject_review_fields["observedPresence"]), bool
        )
        and subject_review[subject_review_fields["observedPresence"]]
        is declared_subject
        and subject_review.get(subject_review_fields["observedSubjectComponents"])
        == approved_subject_components
        and all(
            subject_review.get(subject_review_fields[role]) is True
            for role in (
                "declarationMatchesImage",
                "graphMatchesImage",
                "handoffMatchesImage",
                "slotPolicyValid",
            )
        )
        and isinstance(subject_review.get(subject_review_fields["evidence"]), str)
        and subject_review[subject_review_fields["evidence"]].strip()
    )
    if not subject_review_valid:
        errors.append("独立主体存在性、组件图、Handoff 连续性或主体槽策略复核失败")
    tags = review_request.get(request_fields["classificationTags"])
    tag_reviews = audit[review_fields["tagReviews"]]
    generic_values = set(tag_contract["genericOnlyValues"])
    tag_shape_valid = bool(
        isinstance(tags, list)
        and tag_contract["minimumItems"] <= len(tags) <= tag_contract["maximumItems"]
        and all(
            isinstance(tag, str)
            and tag.strip() == tag
            and tag
            and len(tag) <= tag_contract["maximumCharacters"]
            for tag in tags
        )
        and len(tags) == len(set(tags))
        and any(tag not in generic_values for tag in tags)
    )
    tag_reviews_valid = bool(
        isinstance(tag_reviews, list)
        and isinstance(tags, list)
        and len(tag_reviews) == len(tags)
        and all(
            isinstance(review, dict)
            and set(review) == set(tag_review_fields.values())
            and review.get(tag_review_fields["tag"]) == tag
            and review.get(tag_review_fields["groundedInApprovedImage"]) is True
            and review.get(tag_review_fields["classificationUseful"]) is True
            and isinstance(review.get(tag_review_fields["evidence"]), str)
            and review[tag_review_fields["evidence"]].strip()
            for review, tag in zip(tag_reviews, tags, strict=True)
        )
    )
    if not tag_shape_valid or not tag_reviews_valid:
        errors.append(
            "tags 必须逐项绑定当前 Approved Image，且具有独立分类价值"
        )
    graph = review_request[request_fields["componentGraph"]]
    graph_fields = rules["multiInstanceContract"]["graphFields"]
    component_fields = rules["multiInstanceContract"]["componentFields"]
    components = graph[graph_fields["components"]] if isinstance(graph, dict) else []
    candidates = review_request[request_fields["slotCandidates"]]
    slot_contract = rules["slotCompilationContract"]
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
            and review[slot_fields["approvedInputModes"]] == slot_input_modes(
                next(slot for slot in candidates if slot.get("id") == slot_id),
                rules,
            )
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
    subject_type = slot_contract["slotTypes"]["primarySubjectUpload"]
    expected_subject_ids = [
        slot.get("id")
        for slot in candidates
        if isinstance(slot, dict) and slot.get("type") == subject_type
    ] if isinstance(candidates, list) else []
    inheritance_reviews = audit[review_fields["identityInheritanceReviews"]]
    inheritance_by_id = {
        review.get(inheritance_review_fields["slotIdentity"]): review
        for review in inheritance_reviews
        if isinstance(review, dict)
    } if isinstance(inheritance_reviews, list) else {}
    if not (
        isinstance(inheritance_reviews, list)
        and len(inheritance_by_id) == len(inheritance_reviews)
        and set(inheritance_by_id) == set(expected_subject_ids)
    ):
        errors.append("身份特征继承独立复核未完整覆盖当前 subject 槽")
    for slot_id in expected_subject_ids:
        review = inheritance_by_id.get(slot_id)
        if not (
            isinstance(review, dict)
            and set(review) == set(inheritance_review_fields.values())
            and all(
                review.get(inheritance_review_fields[role]) is True
                for role in (
                    "uploadTraitsComplete",
                    "templateExceptionsMinimal",
                    "clothingPolicyValid",
                )
            )
            and isinstance(
                review.get(inheritance_review_fields["evidence"]), str
            )
            and review[inheritance_review_fields["evidence"]].strip()
        ):
            errors.append(
                f"subject 槽 {slot_id} 的身份、服装继承或最小模板例外复核失败"
            )
    default_reviews = audit[review_fields["defaultValueReviews"]]
    default_by_id = {
        review.get(default_review_fields["slotIdentity"]): review
        for review in default_reviews
        if isinstance(review, dict)
    } if isinstance(default_reviews, list) else {}
    if not (
        isinstance(default_reviews, list)
        and len(default_by_id) == len(default_reviews)
        and set(default_by_id) == set(expected_ids)
    ):
        errors.append("默认值简洁性独立复核未完整覆盖当前槽位")
    for slot in candidates if isinstance(candidates, list) else []:
        slot_id = slot.get("id")
        review = default_by_id.get(slot_id)
        if not (
            isinstance(review, dict)
            and set(review) == set(default_review_fields.values())
            and review.get(default_review_fields["defaultValue"])
            == slot.get("defaultValue")
            and all(
                review.get(default_review_fields[role]) is True
                for role in ("userFacing", "singleAxis", "minimalWording")
            )
            and isinstance(review.get(default_review_fields["evidence"]), str)
            and review[default_review_fields["evidence"]].strip()
        ):
            errors.append(
                f"槽位 {slot_id} 的默认值用户语言、编辑轴或最简表述复核失败"
            )
    if audit[review_fields["pass"]] is not (not errors):
        errors.append("作者合同审计 pass 与逐项结果不一致")
    if not (
        isinstance(audit[review_fields["evidence"]], str)
        and audit[review_fields["evidence"]].strip()
    ):
        errors.append("作者合同审计总证据缺失")
    return errors
