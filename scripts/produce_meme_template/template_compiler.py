from __future__ import annotations

import copy
import re
import unicodedata
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json as _load_json,
    pretty_json_bytes as _json_bytes,
    sha256_bytes as _sha_bytes,
)
from .workflow import (
    CJK_CHARACTER,
    GALLERY_SCHEMA_PATH,
    PLACEHOLDER,
    PLACEHOLDER_WITH_DEFAULT,
    SLOT_ID,
    SUBJECT_IMAGE_MAX_COUNT,
    VISIBLE_TEXT_LEXEME,
    _complete_typed_relation_chain,
    _component_graph_view,
    _deep_keys,
    _deep_strings,
    _forbidden_formal_values,
    _identity_relations_are_consistent,
    _public_asset_url_valid,
    _stop,
)


def _text_tokens_follow_source(
    source_text: str, tokens: list[str], common_punctuation: set[str]
) -> bool:
    cursor = 0
    for token in tokens:
        position = source_text.find(token, cursor)
        if position < 0:
            return False
        cursor = position + len(token)
    normalize = lambda value: "".join(
        character
        for character in value
        if not character.isspace() and character not in common_punctuation
    )
    return normalize("".join(tokens)) == normalize(source_text)


def _normalized_visible_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


def _validate_visible_text_contract(
    analysis: dict[str, Any], slots: list[dict[str, Any]], rules: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    contract = rules["visibleTextContract"]
    analysis_fields = contract["analysisFields"]
    inventory_fields = contract["inventoryFields"]
    region_fields = contract["regionFields"]
    evidence_fields = contract["exactEvidenceFields"]
    actions = contract["actions"]
    roles = set(contract["roles"].values())
    value_classes = set(contract["valueClasses"].values())
    language_values = contract["languageValues"]
    regions = analysis.get(analysis_fields["regions"])
    inventory = analysis.get(analysis_fields["inventory"])

    inventory_valid = bool(
        isinstance(regions, list)
        and isinstance(inventory, dict)
        and set(inventory) == set(inventory_fields.values())
        and inventory.get(inventory_fields["complete"]) is True
        and isinstance(inventory.get(inventory_fields["regionIdentities"]), list)
        and all(
            isinstance(value, str) and value.strip()
            for value in inventory.get(inventory_fields["regionIdentities"], [])
        )
        and len(inventory.get(inventory_fields["regionIdentities"], []))
        == len(set(inventory.get(inventory_fields["regionIdentities"], [])))
        and isinstance(inventory.get(inventory_fields["explanation"]), str)
        and inventory[inventory_fields["explanation"]].strip()
    )
    if not inventory_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见文字清单必须声明完整区域集合并提供非空证据。",
            {},
        )

    region_ids: list[str] = []
    malformed_region_ids: list[str] = []
    invalid_fidelity_ids: list[str] = []
    invalid_route_ids: list[str] = []
    review_region_ids: list[str] = []
    removal_region_ids: list[str] = []
    slot_bindings: dict[str, str] = {}
    common_punctuation = set(contract["commonPunctuationCharacters"])
    for index, region in enumerate(regions):
        fallback_id = f"region-{index}"
        if not isinstance(region, dict):
            malformed_region_ids.append(fallback_id)
            continue
        region_id = region.get(region_fields["identity"])
        source_text = region.get(region_fields["sourceText"])
        role = region.get(region_fields["role"])
        value_class = region.get(region_fields["valueClass"])
        action = region.get(region_fields["action"])
        selected_text = region.get(region_fields["selectedText"])
        evidence = region.get(region_fields["exactTextEvidence"])
        base_region_fields = set(region_fields.values()) - {region_fields["slotIdentity"]}
        expected_region_fields = base_region_fields | (
            {region_fields["slotIdentity"]}
            if action == actions["openSlot"]
            else set()
        )
        if not (
            isinstance(region_id, str)
            and region_id.strip()
            and isinstance(source_text, str)
            and source_text.strip()
            and isinstance(role, str)
            and role in roles
            and isinstance(value_class, str)
            and value_class in value_classes
            and isinstance(action, str)
            and action in set(contract["actions"].values())
            and isinstance(selected_text, str)
            and isinstance(evidence, dict)
            and set(evidence) == set(evidence_fields.values())
            and set(region) == expected_region_fields
        ):
            malformed_region_ids.append(region_id if isinstance(region_id, str) else fallback_id)
            continue
        region_ids.append(region_id)

        language = evidence.get(evidence_fields["language"])
        tokens = evidence.get(evidence_fields["tokens"])
        lines = evidence.get(evidence_fields["lines"])
        case_tokens = evidence.get(evidence_fields["caseSensitiveTokens"])
        rare_symbols = evidence.get(evidence_fields["rareSymbols"])
        source_has_cjk = bool(CJK_CHARACTER.search(source_text))
        source_has_latin = bool(re.search(r"[A-Za-z]", source_text))
        source_has_kana = bool(re.search(contract["japaneseKanaPattern"], source_text))
        source_has_hangul = bool(re.search(contract["koreanHangulPattern"], source_text))
        if source_has_latin and (source_has_cjk or source_has_kana or source_has_hangul):
            allowed_languages = {language_values["mixed"]}
        elif source_has_kana:
            allowed_languages = {language_values["japanese"]}
        elif source_has_hangul:
            allowed_languages = {language_values["korean"]}
        elif source_has_cjk:
            allowed_languages = {
                language_values["simplifiedChinese"],
                language_values["traditionalChinese"],
            }
        elif source_has_latin:
            allowed_languages = {language_values["english"]}
        else:
            allowed_languages = {language_values["undetermined"]}
        expected_case_tokens = {
            token for token in (tokens if isinstance(tokens, list) else [])
            if isinstance(token, str) and re.search(r"[A-Za-z]", token)
        }
        expected_rare_symbols = {
            character
            for character in source_text
            if not character.isalnum()
            and not character.isspace()
            and character not in common_punctuation
        }
        fidelity_valid = bool(
            language in allowed_languages
            and isinstance(tokens, list)
            and tokens
            and all(isinstance(value, str) and value for value in tokens)
            and _text_tokens_follow_source(source_text, tokens, common_punctuation)
            and isinstance(lines, list)
            and lines == source_text.splitlines()
            and isinstance(case_tokens, list)
            and all(isinstance(value, str) and value in source_text for value in case_tokens)
            and len(case_tokens) == len(set(case_tokens))
            and set(case_tokens) == expected_case_tokens
            and isinstance(rare_symbols, list)
            and all(isinstance(value, str) and len(value) == 1 for value in rare_symbols)
            and len(rare_symbols) == len(set(rare_symbols))
            and set(rare_symbols) == expected_rare_symbols
            and isinstance(evidence.get(evidence_fields["symbolTopology"]), str)
            and evidence[evidence_fields["symbolTopology"]].strip()
            and isinstance(evidence.get(evidence_fields["explanation"]), str)
            and evidence[evidence_fields["explanation"]].strip()
        )
        if not fidelity_valid:
            invalid_fidelity_ids.append(region_id)

        if action not in contract["allowedActionsByRole"].get(role, []):
            invalid_route_ids.append(region_id)
        elif action == actions["remove"]:
            removal_region_ids.append(region_id)
        if action == actions["review"]:
            review_region_ids.append(region_id)
        if action == actions["openSlot"]:
            slot_id = region.get(region_fields["slotIdentity"])
            if (
                value_class not in set(contract["openSlotValueClasses"])
                or not isinstance(slot_id, str)
                or not slot_id.strip()
                or not selected_text
                or selected_text not in source_text
                or (
                    value_class == contract["valueClasses"]["highValueSpan"]
                    and selected_text == source_text
                )
                or (
                    selected_text == source_text
                    and len(source_text) > contract["wholeRegionSlotHardMaximum"]
                )
            ):
                invalid_route_ids.append(region_id)
            elif slot_id in slot_bindings:
                invalid_route_ids.append(region_id)
            else:
                slot_bindings[slot_id] = region_id
        elif action == actions["freeEditable"]:
            if (
                value_class not in set(contract["freeEditableValueClasses"])
                or not selected_text
                or selected_text != source_text
            ):
                invalid_route_ids.append(region_id)
        elif action == actions["preserve"] and selected_text != source_text:
            invalid_route_ids.append(region_id)
        elif action == actions["remove"] and selected_text:
            invalid_route_ids.append(region_id)
        elif action == actions["review"] and selected_text != source_text:
            invalid_route_ids.append(region_id)
        elif value_class == contract["valueClasses"]["secondaryReadable"]:
            invalid_route_ids.append(region_id)
        elif value_class in set(contract["nonSlotValueClasses"]) and action not in {
            actions["preserve"],
            actions["remove"],
            actions["review"],
        }:
            invalid_route_ids.append(region_id)
        if action != actions["openSlot"] and region_fields["slotIdentity"] in region:
            invalid_route_ids.append(region_id)

    expected_region_ids = inventory[inventory_fields["regionIdentities"]]
    if (
        malformed_region_ids
        or len(region_ids) != len(set(region_ids))
        or set(region_ids) != set(expected_region_ids)
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见文字区域未被唯一、完整地分类。",
            {"malformedRegionIds": malformed_region_ids},
        )
    if invalid_fidelity_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见文字的原语种、token、换行、大小写或符号拓扑证据与模板图文字不一致。",
            {inventory_fields["regionIdentities"]: sorted(set(invalid_fidelity_ids))},
        )
    if removal_region_ids:
        raise _stop(
            rules,
            "blocked",
            "visualHardFailure",
            "Approved Template Image 仍含需要清理的可见文字，必须修正模板图后重新分析。",
            {inventory_fields["regionIdentities"]: sorted(set(removal_region_ids))},
        )
    if review_region_ids:
        raise _stop(
            rules,
            "needs_input",
            "riskNeedsReview",
            "可见文字角色或处理方式存在歧义，需要人工复核。",
            {inventory_fields["regionIdentities"]: sorted(set(review_region_ids))},
        )
    if invalid_route_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "文字角色、价值类别与 preserve/remove/open/free-editable 操作不兼容。",
            {inventory_fields["regionIdentities"]: sorted(set(invalid_route_ids))},
        )

    text_slot_type = rules["slotCompilationContract"]["slotTypes"]["visibleTextPrompt"]
    binding_field = contract["slotBindingField"]
    text_slots = [slot for slot in slots if slot.get("type") == text_slot_type]
    binding_valid = bool(
        len(text_slots) == len(slot_bindings)
        and all(
            isinstance(slot.get(binding_field), str)
            and slot_bindings.get(slot["id"]) == slot[binding_field]
            and slot.get("defaultValue")
            == next(
                region[region_fields["selectedText"]]
                for region in regions
                if region[region_fields["identity"]] == slot[binding_field]
            )
            for slot in text_slots
        )
    )
    if not binding_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "文字槽必须与一个高价值文字区域和实际选中文字双向绑定。",
            {},
        )
    over_capacity_text_slots = sorted(
        slot["id"]
        for slot in text_slots
        if any(
            not isinstance(value, str)
            or len(value.strip()) > contract["wholeRegionSlotHardMaximum"]
            for value in slot.get("suggestions", [])
        )
    )
    if over_capacity_text_slots:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "文字槽推荐项超出可稳定排版的短文字容量。",
            {"slotIds": over_capacity_text_slots},
        )
    subject_role = rules["slotCompilationContract"]["semanticRoles"]["primarySubject"]
    subject_open = any(slot.get("semanticRole") == subject_role for slot in slots)
    identity_value_class = contract["valueClasses"]["identityRelated"]

    prompt_template = analysis.get("promptTemplate")
    free_editable = analysis.get("freeEditableContent")
    invalid_free_editable_ids = sorted(
        region[region_fields["identity"]]
        for region in regions
        if region[region_fields["action"]] == actions["freeEditable"]
        and (
            not isinstance(prompt_template, str)
            or region[region_fields["selectedText"]] not in prompt_template
            or not isinstance(free_editable, list)
            or region[region_fields["selectedText"]] not in free_editable
        )
    )
    if invalid_free_editable_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "次要可读文字必须同时进入 Prompt Template 与自由编辑内容。",
            {inventory_fields["regionIdentities"]: invalid_free_editable_ids},
        )
    slot_user_values = [
        value
        for slot in slots
        for value in [slot.get("defaultValue"), *slot.get("suggestions", [])]
        if isinstance(value, str)
    ]
    user_editable_texts = [
        prompt_template if isinstance(prompt_template, str) else "",
        *(free_editable if isinstance(free_editable, list) else []),
        *slot_user_values,
    ]
    normalized_user_editable_texts = [
        _normalized_visible_text(value)
        for value in user_editable_texts
        if isinstance(value, str)
    ]

    def forbidden_region_fragments(region: dict[str, Any]) -> tuple[str, set[str]]:
        evidence = region[region_fields["exactTextEvidence"]]
        source = _normalized_visible_text(region[region_fields["sourceText"]])
        lexical_spans = VISIBLE_TEXT_LEXEME.findall(region[region_fields["sourceText"]])
        tokens = {
            normalized
            for value in [*evidence[evidence_fields["tokens"]], *lexical_spans]
            if isinstance(value, str)
            for normalized in [_normalized_visible_text(value)]
            if len(normalized) >= 2
        }
        return source, tokens

    def fixed_region_reenters_user_content(region: dict[str, Any]) -> bool:
        source, tokens = forbidden_region_fragments(region)
        return bool(
            (source and any(source in value for value in normalized_user_editable_texts))
            or any(token == value for token in tokens for value in normalized_user_editable_texts)
        )

    forbidden_user_text_region_ids = sorted(
        region[region_fields["identity"]]
        for region in regions
        if region[region_fields["action"]] not in {
            actions["openSlot"],
            actions["freeEditable"],
        }
        and not (
            region[region_fields["valueClass"]] == identity_value_class
            and not subject_open
        )
        and fixed_region_reenters_user_content(region)
    )
    if forbidden_user_text_region_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "固定、归因、品牌或清理文字不能重新进入用户 Prompt、自由编辑内容或普通槽位。",
            {inventory_fields["regionIdentities"]: forbidden_user_text_region_ids},
        )
    return copy.deepcopy(regions), copy.deepcopy(inventory)


def _compile_editable_spec(
    analysis: dict[str, Any], rules: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    slot_contract = rules["slotCompilationContract"]
    value_gate_roles = tuple(slot_contract["valueGateRoles"].values())
    allowed_semantic_roles = {
        *slot_contract["semanticRoles"].values(),
        *slot_contract["personAttributeRoles"].values(),
    }
    subject_role = slot_contract["semanticRoles"]["primarySubject"]
    subject_upload_type = slot_contract["slotTypes"]["primarySubjectUpload"]
    slot_candidates = analysis.get("slotCandidates")
    slot_candidates_valid = bool(
        isinstance(slot_candidates, list)
        and all(
            isinstance(slot, dict)
            and isinstance(slot.get("id"), str)
            and SLOT_ID.fullmatch(slot["id"])
            and isinstance(slot.get("semanticRole"), str)
            and slot["semanticRole"] in allowed_semantic_roles
            and slot.get("type") in set(slot_contract["slotTypes"].values())
            and isinstance(slot.get("defaultValue"), str)
            and isinstance(slot.get("suggestions"), list)
            and (slot["type"] == subject_upload_type)
            is (slot["semanticRole"] == subject_role)
            and isinstance(slot.get("valueGates"), dict)
            and set(slot["valueGates"]) == set(value_gate_roles)
            and all(isinstance(slot["valueGates"][role], bool) for role in value_gate_roles)
            for slot in slot_candidates
        )
        and len({slot["id"] for slot in slot_candidates}) == len(slot_candidates)
    )
    if not slot_candidates_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位候选必须提供合法默认值、推荐池和四道具名价值门禁。",
            {},
        )
    slots = [
        copy.deepcopy(slot)
        for slot in slot_candidates
        if all(slot["valueGates"][role] for role in value_gate_roles)
    ]
    identity_contract = rules["identityReplacementContract"]
    identity_plan_fields = identity_contract["planFields"]
    if identity_plan_fields["route"] in plan:
        decision_fields = identity_contract["identityTextDecisionFields"]
        actions = identity_contract["identityTextActions"]
        identity_text_role = slot_contract["semanticRoles"]["identityText"]
        exposed_defaults = {
            decision[decision_fields["result"]]
            for decision in plan[identity_plan_fields["textDecisions"]]
            if decision[decision_fields["action"]] == actions["exposeNeutralSlot"]
        }
        exposed_slots = [
            slot for slot in slots if slot.get("semanticRole") == identity_text_role
        ]
        exposed_slot_defaults = {slot.get("defaultValue") for slot in exposed_slots}
        exposed_slots_are_text = all(
            slot.get("type") == slot_contract["slotTypes"]["visibleTextPrompt"]
            for slot in exposed_slots
        )
        synchronized_text_present = any(
            decision[decision_fields["action"]] == actions["synchronize"]
            for decision in plan[identity_plan_fields["textDecisions"]]
        )
        subject_open = any(
            slot.get("semanticRole") == slot_contract["semanticRoles"]["primarySubject"]
            for slot in slots
        )
        if not exposed_slots_are_text or exposed_defaults != exposed_slot_defaults or (
            synchronized_text_present and subject_open
        ):
            raise _stop(
                rules,
                "blocked",
                "contractFailure",
                "身份文字的中性文字槽与 Replacement Plan 不一致，或具体身份文字与开放主体同时存在。",
                {},
            )
    text_regions, visible_text_inventory = _validate_visible_text_contract(
        analysis, slots, rules
    )
    budget = rules["slotBudget"]
    has_primary_subject = analysis.get("hasPrimarySubject")
    subject_kind = analysis.get("subjectKind")
    person_kind = slot_contract["subjectKinds"]["humanSubject"]
    discriminator_valid = bool(
        isinstance(has_primary_subject, bool)
        and subject_kind in set(slot_contract["subjectKinds"].values())
        and (subject_kind != person_kind or has_primary_subject)
    )
    if not discriminator_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "主体存在性与人物/非人物判别必须提供完整且一致的具名结论。",
            {},
        )
    single_slot_evidence = analysis.get("singleSlotExceptionEvidence")
    single_slot_valid = bool(
        len(slots) == 1
        and isinstance(single_slot_evidence, dict)
        and single_slot_evidence.get("confirmedOnlyOneHighValue") is True
        and isinstance(single_slot_evidence.get("reviewedAxes"), list)
        and all(isinstance(value, str) for value in single_slot_evidence["reviewedAxes"])
        and len(single_slot_evidence["reviewedAxes"])
        == len(set(single_slot_evidence["reviewedAxes"]))
        and set(single_slot_evidence.get("reviewedAxes", []))
        == set(slot_contract["singleSlotReviewAxes"].values())
        and isinstance(single_slot_evidence.get("reason"), str)
        and single_slot_evidence["reason"].strip()
    )
    within_budget = budget["minimum"] <= len(slots) <= budget["maximum"]
    if not within_budget and not single_slot_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "高价值槽位数量不在常态预算内。",
            {"slotCount": len(slots)},
        )
    if has_primary_subject and not any(slot["semanticRole"] == subject_role for slot in slots):
        omission = analysis.get("subjectSlotOmissionEvidence")
        omission_valid = bool(
            isinstance(omission, dict)
            and omission.get("reviewed") is True
            and isinstance(omission.get("valueGates"), dict)
            and set(omission["valueGates"]) == set(value_gate_roles)
            and all(isinstance(value, bool) for value in omission["valueGates"].values())
            and not all(omission["valueGates"].values())
            and isinstance(omission.get("reason"), str)
            and omission["reason"].strip()
        )
        if not omission_valid:
            raise _stop(
                rules,
                "blocked",
                "contractFailure",
                "画面存在明显主体，但高价值槽位没有主体入口或省略证据无效。",
                {},
            )
    if subject_kind == person_kind:
        assessments = analysis.get("subjectAttributeAssessments")
        attribute_roles = set(slot_contract["personAttributeRoles"].values())
        assessment_valid = bool(
            isinstance(assessments, dict)
            and set(assessments) == attribute_roles
            and all(
                isinstance(item, dict)
                and set(item) == {*value_gate_roles, "includedAsSlot", "evidence"}
                and all(isinstance(item.get(gate), bool) for gate in value_gate_roles)
                and isinstance(item.get("includedAsSlot"), bool)
                and isinstance(item.get("evidence"), str)
                and item["evidence"].strip()
                for item in assessments.values()
            )
            and all(
                assessment["includedAsSlot"]
                == all(assessment[gate] for gate in value_gate_roles)
                and assessment["includedAsSlot"]
                == (sum(slot.get("semanticRole") == role for slot in slots) == 1)
                for role, assessment in assessments.items()
            )
        )
        if not assessment_valid:
            raise _stop(
                rules,
                "blocked",
                "contractFailure",
                "人物服装、造型、发型、姿势和颜色缺少独立价值与稳定性评估。",
                {},
            )
    asset_units = analysis.get("assetUnitAnalysis")
    multi_contract = rules["multiInstanceContract"]
    approved_graph = analysis.get(
        multi_contract["approvedFields"]["componentGraph"]
    )
    approved_graph_view = _component_graph_view(approved_graph, rules)
    count_fields = set(slot_contract["assetUnitCountFields"].values())
    control_count_field = slot_contract["assetUnitCountFields"]["controls"]
    component_fields = multi_contract["componentFields"]
    components = approved_graph_view[0] if approved_graph_view is not None else []
    approved_relations = approved_graph_view[1] if approved_graph_view is not None else []
    computed_visible_count = sum(
        component[component_fields["visualInstance"]] is True for component in components
    )
    computed_identity_ids = {
        component[component_fields["identityUnit"]]
        for component in components
        if component[component_fields["identityUnit"]] is not None
    }
    computed_upload_ids = {
        component[component_fields["uploadAsset"]]
        for component in components
        if component[component_fields["uploadAsset"]] is not None
    }
    computed_control_ids = {
        component[component_fields["control"]]
        for component in components
        if component[component_fields["control"]] is not None
    }
    slot_by_id = {slot["id"]: slot for slot in slots}
    semantic_role_by_key = {
        **slot_contract["semanticRoles"],
        **slot_contract["personAttributeRoles"],
    }
    allowed_control_bindings = {
        multi_contract["componentRoles"][component_role]: {
            (
                slot_contract["slotTypes"][binding["slotTypeRole"]],
                semantic_role_by_key[binding["semanticRoleKey"]],
            )
            for binding in bindings
        }
        for component_role, bindings in multi_contract[
            "approvedControlBindings"
        ].items()
    }
    uploads_by_control: dict[str, set[str]] = {}
    controls_by_upload: dict[str, set[str]] = {}
    for component in components:
        upload_id = component[component_fields["uploadAsset"]]
        control_id = component[component_fields["control"]]
        if upload_id is None or control_id is None:
            continue
        uploads_by_control.setdefault(control_id, set()).add(upload_id)
        controls_by_upload.setdefault(upload_id, set()).add(control_id)
    graph_counts = {
        slot_contract["assetUnitCountFields"]["visibleSubjects"]: computed_visible_count,
        slot_contract["assetUnitCountFields"]["identities"]: len(computed_identity_ids),
        slot_contract["assetUnitCountFields"]["uploads"]: len(computed_upload_ids),
        slot_contract["assetUnitCountFields"]["controls"]: len(computed_control_ids),
    }
    relation_fields = multi_contract["relationFields"]
    approved_component_by_id = {
        component[component_fields["identity"]]: component for component in components
    }
    approved_identity_relations_valid = _identity_relations_are_consistent(
        components, approved_relations, multi_contract
    )
    allowed_relation_role_pairs = {
        multi_contract["relationTypes"][relation_role]: {
            (
                multi_contract["componentRoles"][source_role],
                multi_contract["componentRoles"][target_role],
            )
            for source_role, target_role in role_pairs
        }
        for relation_role, role_pairs in multi_contract[
            "relationEndpointRoleKeyPairs"
        ].items()
    }
    approved_relation_roles_valid = all(
        (
            approved_component_by_id[relation[relation_fields["source"]]][
                component_fields["role"]
            ],
            approved_component_by_id[relation[relation_fields["target"]]][
                component_fields["role"]
            ],
        )
        in allowed_relation_role_pairs[relation[relation_fields["type"]]]
        for relation in approved_relations
    )
    approved_relation_types_by_component: dict[str, set[str]] = {
        component_id: set() for component_id in approved_component_by_id
    }
    for relation in approved_relations:
        approved_relation_types_by_component[relation[relation_fields["source"]]].add(
            relation[relation_fields["type"]]
        )
        approved_relation_types_by_component[relation[relation_fields["target"]]].add(
            relation[relation_fields["type"]]
        )
    identity_contract = rules["identityReplacementContract"]
    component_required_relation_types: dict[str, set[str]] = {}
    for dependency_role in multi_contract["approvedIdentityDependencyRoleKeys"]:
        required_relation_types = {
            multi_contract["relationTypes"][relation_role]
            for relation_role in identity_contract["dependencyRelationTypeKeys"][
                dependency_role
            ]
        }
        for component_role in identity_contract["dependencyComponentRoleKeys"][
            dependency_role
        ]:
            component_required_relation_types.setdefault(
                multi_contract["componentRoles"][component_role], set()
            ).update(required_relation_types)
    approved_component_relations_complete = all(
        component[component_fields["role"]]
        not in component_required_relation_types
        or bool(
            component_required_relation_types[component[component_fields["role"]]]
            & approved_relation_types_by_component[
                component[component_fields["identity"]]
            ]
        )
        for component in components
    )
    plan_operations = plan.get(multi_contract["planFields"]["imageOperations"], [])
    source_plan_graph_view = _component_graph_view(
        plan.get(multi_contract["planFields"]["componentGraph"]), rules
    )
    source_plan_components = (
        source_plan_graph_view[0] if source_plan_graph_view is not None else []
    )
    source_plan_relations = (
        source_plan_graph_view[1] if source_plan_graph_view is not None else []
    )
    source_plan_component_by_id = {
        component[component_fields["identity"]]: component
        for component in source_plan_components
    }
    source_plan_relation_by_id = {
        relation[relation_fields["identity"]]: relation
        for relation in source_plan_relations
    }
    operation_fields = multi_contract["operationFields"]
    operation_role_by_value = {
        value: role for role, value in multi_contract["operations"].items()
    }
    binding_fields = multi_contract["approvedOperationBindingFields"]
    approved_bindings = analysis.get(
        multi_contract["approvedFields"]["operationBindings"]
    )
    component_id_set = set(approved_component_by_id)
    plan_operation_ids = {
        operation[operation_fields["identity"]] for operation in plan_operations
    }
    approved_bindings_shape_valid = bool(
        isinstance(approved_bindings, list)
        and len(approved_bindings) == len(plan_operations)
        and all(
            isinstance(binding, dict)
            and set(binding) == set(binding_fields.values())
            and isinstance(binding.get(binding_fields["operationIdentity"]), str)
            and binding[binding_fields["operationIdentity"]].strip()
            and all(
                isinstance(binding.get(binding_fields[field]), list)
                and all(
                    isinstance(value, str) and value.strip()
                    for value in binding[binding_fields[field]]
                )
                and len(binding[binding_fields[field]])
                == len(set(binding[binding_fields[field]]))
                for field in ("targetComponents", "stableAnchors", "controls")
            )
            and binding[binding_fields["targetComponents"]]
            and binding[binding_fields["stableAnchors"]]
            and set(binding[binding_fields["targetComponents"]]) <= component_id_set
            and set(binding[binding_fields["stableAnchors"]]) <= component_id_set
            and not (
                set(binding[binding_fields["targetComponents"]])
                & set(binding[binding_fields["stableAnchors"]])
            )
            and set(binding[binding_fields["controls"]]) <= set(slot_by_id)
            and isinstance(binding.get(binding_fields["explanation"]), str)
            and binding[binding_fields["explanation"]].strip()
            for binding in approved_bindings
        )
        and {
            binding[binding_fields["operationIdentity"]]
            for binding in approved_bindings
        }
        == plan_operation_ids
    )
    if approved_bindings_shape_valid:
        approved_target_ids = [
            component_id
            for binding in approved_bindings
            for component_id in binding[binding_fields["targetComponents"]]
        ]
        approved_bindings_shape_valid = len(approved_target_ids) == len(
            set(approved_target_ids)
        )
    approved_binding_by_operation = (
        {
            binding[binding_fields["operationIdentity"]]: binding
            for binding in approved_bindings
        }
        if approved_bindings_shape_valid
        else {}
    )

    def approved_operation_is_complete(operation: dict[str, Any]) -> bool:
        binding = approved_binding_by_operation[
            operation[operation_fields["identity"]]
        ]
        operation_role = operation_role_by_value[
            operation[operation_fields["operation"]]
        ]
        requirement = multi_contract["operationRequirements"][operation_role]
        source_targets = [
            source_plan_component_by_id[target_id]
            for target_id in operation[operation_fields["targetRegions"]]
        ]
        source_anchors = [
            source_plan_component_by_id[anchor_id]
            for anchor_id in operation[operation_fields["stableAnchors"]]
        ]
        selected_targets = [
            approved_component_by_id[component_id]
            for component_id in binding[binding_fields["targetComponents"]]
        ]
        selected_anchors = [
            approved_component_by_id[component_id]
            for component_id in binding[binding_fields["stableAnchors"]]
        ]
        if (
            [component[component_fields["role"]] for component in selected_targets]
            != [component[component_fields["role"]] for component in source_targets]
            or [component[component_fields["role"]] for component in selected_anchors]
            != [component[component_fields["role"]] for component in source_anchors]
        ):
            return False
        if operation_role == "identityReplace":
            selected_identity_units = {
                component[component_fields["identityUnit"]]
                for component in selected_targets
            }
            if None in selected_identity_units or len(selected_identity_units) != 1:
                return False
            selected_identity = next(iter(selected_identity_units))
            if {
                component[component_fields["identity"]]
                for component in components
                if component[component_fields["identityUnit"]] == selected_identity
            } != {
                component[component_fields["identity"]]
                for component in selected_targets
            }:
                return False
        selected_ids = {
            component[component_fields["identity"]] for component in selected_targets
        }
        selected_anchor_ids = {
            component[component_fields["identity"]] for component in selected_anchors
        }
        selected_control_ids = {
            component[component_fields["control"]]
            for component in selected_targets
            if component[component_fields["control"]] is not None
        }
        components_using_selected_controls = {
            component[component_fields["identity"]]
            for component in components
            if component[component_fields["control"]] in selected_control_ids
        }
        requires_control = operation_role != "identityReplace" or any(
            slot["type"] == subject_upload_type for slot in slots
        )
        if selected_control_ids != set(binding[binding_fields["controls"]]) or (
            requires_control and not selected_control_ids
        ) or not components_using_selected_controls <= selected_ids:
            return False
        selected_container_ids = {
            component[component_fields["container"]]
            for component in selected_targets
            if component[component_fields["container"]] is not None
        }
        if requirement["targetContainersMustBeAnchors"] and not (
            selected_container_ids <= selected_anchor_ids
        ):
            return False
        selected_scope_ids = selected_ids | selected_anchor_ids | selected_container_ids
        required_relation_types = {
            multi_contract["relationTypes"][relation_role]
            for relation_role in requirement["requiredRelationTypeKeys"]
        }
        source_preserved_relations = [
            source_plan_relation_by_id[relation_id]
            for relation_id in operation[operation_fields["preservedRelations"]]
        ]
        required_relation_types.update(
            relation[relation_fields["type"]]
            for relation in source_preserved_relations
        )
        ordered_relation_type = multi_contract["relationTypes"]["orderedBefore"]
        scoped_approved_relations = [
            relation
            for relation in approved_relations
            if {
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            }
            <= selected_scope_ids
            and (
                bool(
                    {
                        relation[relation_fields["source"]],
                        relation[relation_fields["target"]],
                    }
                    & selected_ids
                )
                or relation[relation_fields["type"]] == ordered_relation_type
            )
        ]
        if not required_relation_types <= {
            relation[relation_fields["type"]]
            for relation in scoped_approved_relations
        }:
            return False

        source_to_approved_component = {
            **dict(
                zip(
                    operation[operation_fields["targetRegions"]],
                    binding[binding_fields["targetComponents"]],
                )
            ),
            **dict(
                zip(
                    operation[operation_fields["stableAnchors"]],
                    binding[binding_fields["stableAnchors"]],
                )
            ),
        }
        if any(
            relation[relation_fields["source"]] not in source_to_approved_component
            or relation[relation_fields["target"]]
            not in source_to_approved_component
            for relation in source_preserved_relations
        ):
            return False
        source_relation_signatures = [
            (
                relation[relation_fields["type"]],
                source_to_approved_component[relation[relation_fields["source"]]],
                source_to_approved_component[relation[relation_fields["target"]]],
            )
            for relation in source_preserved_relations
        ]
        approved_relation_signatures = [
            (
                relation[relation_fields["type"]],
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            )
            for relation in scoped_approved_relations
        ]
        if any(
            approved_relation_signatures.count(signature)
            < source_relation_signatures.count(signature)
            for signature in set(source_relation_signatures)
        ):
            return False
        if requirement["requiresCompleteOrderedChain"]:
            return _complete_typed_relation_chain(
                selected_container_ids,
                approved_relations,
                multi_contract["relationTypes"]["orderedBefore"],
                relation_fields,
            )
        return True

    approved_operation_topology_complete = bool(
        source_plan_graph_view is not None
        and approved_bindings_shape_valid
        and all(
            approved_operation_is_complete(operation) for operation in plan_operations
        )
    )

    repeated_identity_type = multi_contract["relationTypes"]["repeatedIdentity"]

    def approved_repeated_subjects_are_connected() -> bool:
        subjects_by_identity: dict[str, set[str]] = {}
        for component in components:
            identity_unit = component[component_fields["identityUnit"]]
            if (
                identity_unit is not None
                and component[component_fields["role"]]
                == multi_contract["componentRoles"]["subject"]
            ):
                subjects_by_identity.setdefault(identity_unit, set()).add(
                    component[component_fields["identity"]]
                )
        repeated_edges = [
            relation
            for relation in approved_relations
            if relation[relation_fields["type"]] == repeated_identity_type
        ]
        for subject_ids in subjects_by_identity.values():
            if len(subject_ids) < 2:
                continue
            adjacency = {subject_id: set() for subject_id in subject_ids}
            for relation in repeated_edges:
                source_id = relation[relation_fields["source"]]
                target_id = relation[relation_fields["target"]]
                if source_id in subject_ids and target_id in subject_ids:
                    adjacency[source_id].add(target_id)
                    adjacency[target_id].add(source_id)
            visited: set[str] = set()
            pending = [next(iter(subject_ids))]
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(adjacency[current] - visited)
            if visited != subject_ids:
                return False
        return True
    approved_control_bindings_valid = all(
        component[component_fields["control"]] is None
        or (
            component[component_fields["control"]] in slot_by_id
            and (
                slot_by_id[component[component_fields["control"]]]["type"],
                slot_by_id[component[component_fields["control"]]]["semanticRole"],
            )
            in allowed_control_bindings.get(
                component[component_fields["role"]], set()
            )
        )
        for component in components
    )
    approved_graph_valid = bool(
        approved_graph_view is not None
        and approved_identity_relations_valid
        and approved_relation_roles_valid
        and approved_component_relations_complete
        and approved_operation_topology_complete
        and approved_repeated_subjects_are_connected()
        and approved_control_bindings_valid
        and computed_control_ids == {slot["id"] for slot in slots}
        and all(
            not component[component_fields["visualInstance"]]
            or component[component_fields["identityUnit"]] is not None
            for component in components
        )
        and all(
            component[component_fields["uploadAsset"]] is None
            or (
                component[component_fields["control"]] in slot_by_id
                and slot_by_id[component[component_fields["control"]]]["type"]
                == subject_upload_type
            )
            for component in components
        )
        and all(len(control_ids) == 1 for control_ids in controls_by_upload.values())
        and all(
            slot["type"] != subject_upload_type
            or bool(uploads_by_control.get(slot["id"]))
            for slot in slots
        )
        and all(
            len(upload_ids) <= SUBJECT_IMAGE_MAX_COUNT
            for upload_ids in uploads_by_control.values()
        )
    )
    asset_units_valid = bool(
        approved_graph_valid
        and isinstance(asset_units, dict)
        and set(asset_units) == {*count_fields, "evidence"}
        and all(
            isinstance(asset_units[field], int)
            and not isinstance(asset_units[field], bool)
            and asset_units[field] >= 0
            for field in count_fields
        )
        and all(asset_units[field] == value for field, value in graph_counts.items())
        and asset_units[control_count_field] == len(slots)
        and isinstance(asset_units.get("evidence"), str)
        and asset_units["evidence"].strip()
    )
    if not asset_units_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "组件图无效，或画面实例、身份、上传素材和控件四类数量没有独立准确计算。",
            {},
        )
    image_max_count_field = multi_contract["subjectImageMaxCountField"]
    for slot in slots:
        if slot["type"] == subject_upload_type:
            slot[image_max_count_field] = len(uploads_by_control[slot["id"]])
    default_preference = slot_contract["defaultValuePreference"]
    preference_exceptions = analysis.get("defaultValuePreferenceExceptionEvidence", {})
    preference_exceptions_valid = bool(
        isinstance(preference_exceptions, dict)
        and set(preference_exceptions) <= {slot["id"] for slot in slots}
        and all(
            isinstance(evidence, dict)
            and set(evidence) == {"reviewed", "reason"}
            and evidence.get("reviewed") is True
            and isinstance(evidence.get("reason"), str)
            and evidence["reason"].strip()
            for evidence in preference_exceptions.values()
        )
    )
    if not preference_exceptions_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位默认值语言或长度偏好例外证据无效。",
            {},
        )

    def exact_visible_text_evidence_is_valid(slot: dict[str, Any]) -> bool:
        evidence = slot.get("exactVisibleTextEvidence")
        return bool(
            slot.get("type") == slot_contract["slotTypes"]["visibleTextPrompt"]
            and slot.get("exactVisibleText") is True
            and isinstance(evidence, dict)
            and set(evidence) == {"approvedImageSha256", "visibleText", "evidence"}
            and evidence.get("approvedImageSha256") == analysis.get("visualFactSourceSha256")
            and evidence.get("visibleText") == slot.get("defaultValue")
            and isinstance(evidence.get("evidence"), str)
            and evidence["evidence"].strip()
        )

    invalid_exact_text_evidence = sorted(
        slot["id"]
        for slot in slots
        if (
            slot.get("exactVisibleText") is True
            and not exact_visible_text_evidence_is_valid(slot)
        )
        or (
            "exactVisibleTextEvidence" in slot
            and not exact_visible_text_evidence_is_valid(slot)
        )
    )
    if invalid_exact_text_evidence:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "精确画内文字槽必须绑定当前 Approved Template Image、默认值和可见证据。",
            {"slotIds": invalid_exact_text_evidence},
        )
    invalid_defaults = sorted(
        slot["id"]
        for slot in slots
        if not isinstance(slot.get("defaultValue"), str)
        or not slot["defaultValue"].strip()
        or (
            len(slot["defaultValue"].strip()) > default_preference["hardMaximum"]
            and not (
                default_preference["exactVisibleTextMayExceed"]
                and exact_visible_text_evidence_is_valid(slot)
            )
        )
        or (
            not default_preference["preferredMinimum"]
            <= len(slot["defaultValue"].strip())
            <= default_preference["preferredMaximum"]
            and slot["id"] not in preference_exceptions
        )
        or (
            default_preference["preferChinese"]
            and not CJK_CHARACTER.search(slot["defaultValue"].strip())
            and slot["id"] not in preference_exceptions
        )
    )
    if invalid_defaults:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位默认值为空、超出硬上限，或偏离中文与长度偏好但缺少审计例外。",
            {"slotIds": invalid_defaults},
        )
    prompt_template = analysis.get("promptTemplate")
    inline_bindings = PLACEHOLDER_WITH_DEFAULT.findall(prompt_template) if isinstance(prompt_template, str) else []
    inline_defaults_valid = bool(
        isinstance(prompt_template, str)
        and prompt_template.strip()
        and set(PLACEHOLDER.findall(prompt_template)) == {slot["id"] for slot in slots}
        and len(PLACEHOLDER.findall(prompt_template)) == len(inline_bindings)
        and all(
            inline_default == slot["defaultValue"]
            for slot in slots
            for binding_id, inline_default in inline_bindings
            if binding_id == slot["id"]
        )
    )
    if not inline_defaults_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "Prompt Template 的槽位绑定和内联默认值必须与槽位侧车完全一致。",
            {},
        )
    def suggestions_are_valid(slot: dict[str, Any]) -> bool:
        suggestions = slot.get("suggestions")
        normalized_suggestions = (
            [value.strip() for value in suggestions]
            if isinstance(suggestions, list) and all(isinstance(value, str) for value in suggestions)
            else []
        )
        return bool(
            isinstance(suggestions, list)
            and suggestions
            and all(isinstance(value, str) and value.strip() for value in suggestions)
            and len(normalized_suggestions) == len(set(normalized_suggestions))
            and slot.get("defaultValue", "").strip() not in normalized_suggestions
        )

    invalid_suggestion_slots = sorted(slot["id"] for slot in slots if not suggestions_are_valid(slot))
    if invalid_suggestion_slots:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位推荐项包含空值、重复值或默认值。",
            {"slotIds": invalid_suggestion_slots},
        )
    missing_semantic_guards = sorted(
        slot["id"]
        for slot in slots
        if not slot.get("hiddenConflictTokens") or not slot.get("titleForbiddenTokens")
    )
    if missing_semantic_guards:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "高价值槽位缺少隐藏约束冲突词或最大差异标题禁用词。",
            {"slotIds": missing_semantic_guards},
        )
    review_field = rules["formalProjection"]["metadata"]["reviewReason"]
    needs_review = analysis.get(review_field)
    if needs_review is not None and (
        not isinstance(needs_review, str) or not needs_review.strip()
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "needsReview 仅在确有人工复核原因时保留非空字符串。",
            {},
        )
    default_slot_values = {slot["id"]: slot["defaultValue"] for slot in slots}
    editable = {
        "artifactType": "editable-template-spec",
        "schemaVersion": rules["schemaVersion"],
        "visualFactSourceSha256": analysis["visualFactSourceSha256"],
        "title": analysis["neutralTitle"],
        "description": analysis["neutralDescription"],
        "slots": slots,
        "slotSuggestionPools": {slot["id"]: slot["suggestions"] for slot in slots},
        "promptTemplate": prompt_template,
        "freeEditableContent": analysis["freeEditableContent"],
        rules["visibleTextContract"]["analysisFields"]["regions"]: text_regions,
        rules["visibleTextContract"]["analysisFields"]["inventory"]: visible_text_inventory,
        "tags": analysis["tags"],
        "resolvedPromptContract": {
            "singleSourceField": "promptTemplate",
            "defaultSlotValues": default_slot_values,
            "defaultResolvedPrompt": _resolve_prompt(prompt_template, default_slot_values),
        },
    }
    if single_slot_valid:
        editable["singleSlotExceptionEvidence"] = single_slot_evidence
    if has_primary_subject and not any(
        slot["semanticRole"] == subject_role for slot in slots
    ):
        editable["subjectSlotOmissionEvidence"] = analysis["subjectSlotOmissionEvidence"]
    if subject_kind == person_kind:
        editable["subjectAttributeAssessments"] = analysis["subjectAttributeAssessments"]
    editable["assetUnitAnalysis"] = asset_units
    editable[multi_contract["approvedFields"]["componentGraph"]] = copy.deepcopy(
        approved_graph
    )
    if preference_exceptions:
        editable["defaultValuePreferenceExceptionEvidence"] = preference_exceptions
    if needs_review is not None:
        editable[review_field] = needs_review.strip()
    return editable


def _slot_to_input(slot: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    slot_types = rules["slotCompilationContract"]["slotTypes"]
    multi_contract = rules["multiInstanceContract"]
    if slot["type"] == slot_types["primarySubjectUpload"]:
        image_max_count = slot[multi_contract["subjectImageMaxCountField"]]
        return {
            "id": slot["id"],
            "type": slot_types["primarySubjectUpload"],
            "label": slot["label"],
            "required": False,
            "resolutionStrategy": "image_over_text",
            "text": {
                "placeholder": slot["placeholder"],
                "allowCustom": True,
                "defaultValue": slot["defaultValue"],
                "suggestions": slot["suggestions"],
            },
            "image": {
                "enabled": True,
                "promptValue": "用户上传的主体素材",
                "hint": (
                    "上传1张清晰主体图，按模板参考图的媒介与区域职责完整重绘"
                    if image_max_count == 1
                    else f"按画面顺序上传最多{image_max_count}张清晰主体图"
                ),
                "extract": (
                    "提取该主体可辨识的身份特征，并在模板参考图的媒介与造型体系中重绘。"
                    if image_max_count == 1
                    else "按上传顺序逐张提取主体身份特征，并匹配模板中的有序目标区域。"
                ),
                "maxCount": image_max_count,
                "minWidth": 256,
                "minHeight": 256,
                "private": True,
                "sourceOptions": ["upload", "recent_upload", "asset_library"],
            },
        }
    return {
        "id": slot["id"],
        "type": slot_types["freePrompt"],
        "label": slot["label"],
        "placeholder": slot["placeholder"],
        "required": False,
        "suggestions": slot["suggestions"],
    }


def _compile_hidden_spec(analysis: dict[str, Any], editable: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    prompt_enhancement = analysis.get("promptEnhancement")
    instruction = prompt_enhancement.get("instruction") if isinstance(prompt_enhancement, dict) else None
    locked_constraints = (
        prompt_enhancement.get("lockedConstraints") if isinstance(prompt_enhancement, dict) else None
    )
    preserve = prompt_enhancement.get("preserve") if isinstance(prompt_enhancement, dict) else None
    hidden_layers_valid = bool(
        isinstance(instruction, str)
        and instruction.strip()
        and isinstance(locked_constraints, list)
        and locked_constraints
        and all(isinstance(value, str) and value.strip() for value in locked_constraints)
        and len(locked_constraints) == len(set(locked_constraints))
        and isinstance(preserve, list)
        and preserve
        and all(isinstance(value, str) and value.strip() for value in preserve)
        and len(preserve) == len(set(preserve))
        and set(locked_constraints).isdisjoint(preserve)
    )
    if not hidden_layers_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "instruction、lockedConstraints 与 preserve 必须完整，且呈现维度和语义锚点职责不可重复。",
            {},
        )
    forbidden = [term for term in rules["prompt"]["forbiddenInstructionTerms"] if term in instruction]
    if len(instruction) > rules["prompt"]["instructionMaxCharacters"] or forbidden:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "instruction 超出长度限制或包含隐藏层禁用内容。",
            {"characters": len(instruction), "forbiddenTerms": forbidden},
        )
    return {
        "artifactType": "hidden-template-spec",
        "schemaVersion": rules["schemaVersion"],
        "visualFactSourceSha256": analysis["visualFactSourceSha256"],
        "inputSchema": [_slot_to_input(slot, rules) for slot in editable["slots"]],
        "promptEnhancement": {
            "stageKey": "gallery.prompt_rewrite",
            "instruction": instruction,
            "referenceField": "referenceImage",
            "lockedConstraints": locked_constraints,
            "preserve": preserve,
            "output": {"format": "json", "promptField": "finalPrompt"},
        },
    }


def _compile_draft(
    template_key: str,
    image_size: str,
    editable: dict[str, Any],
    hidden: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    formal_contract = rules["formalProjection"]
    top_level = formal_contract["topLevel"]
    tags_field = formal_contract["metadata"]["classificationTags"]
    review_field = formal_contract["metadata"]["reviewReason"]
    metadata = {tags_field: editable["tags"]}
    if review_field in editable:
        metadata[review_field] = editable[review_field]
    return {
        top_level["templateKey"]: template_key,
        top_level["lifecycleStatus"]: formal_contract["statusValues"]["draft"],
        top_level["userTitle"]: editable["title"],
        top_level["userDescription"]: editable["description"],
        top_level["outputImageSize"]: image_size,
        top_level["userPromptTemplate"]: editable["promptTemplate"],
        top_level["userInputSchema"]: hidden["inputSchema"],
        top_level["hiddenPromptEnhancement"]: hidden["promptEnhancement"],
        top_level["formalMetadata"]: metadata,
    }


SENTENCE_PUNCTUATION = re.compile(r"[，。！？；,.!?;]")


def _resolve_prompt(prompt_template: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), prompt_template)


def _semantic_audit_payload(
    draft: dict[str, Any], editable: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    top_level = rules["formalProjection"]["topLevel"]
    text_analysis_fields = rules["visibleTextContract"]["analysisFields"]
    return copy.deepcopy({
        top_level["userTitle"]: draft[top_level["userTitle"]],
        top_level["userPromptTemplate"]: draft[top_level["userPromptTemplate"]],
        top_level["hiddenPromptEnhancement"]: draft[top_level["hiddenPromptEnhancement"]],
        "freeEditableContent": editable["freeEditableContent"],
        text_analysis_fields["regions"]: editable[text_analysis_fields["regions"]],
        text_analysis_fields["inventory"]: editable[text_analysis_fields["inventory"]],
        "slots": [
            {
                "id": slot["id"],
                "type": slot["type"],
                "semanticRole": slot["semanticRole"],
                "label": slot["label"],
                "placeholder": slot["placeholder"],
                "defaultValue": slot["defaultValue"],
                "suggestions": slot["suggestions"],
            }
            for slot in editable["slots"]
        ],
    })


def _validation_report(
    draft: dict[str, Any],
    editable: dict[str, Any],
    plan: dict[str, Any],
    source_analysis: dict[str, Any],
    review: dict[str, Any],
    semantic_audit: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    top_level = rules["formalProjection"]["topLevel"]
    title_field = top_level["userTitle"]
    prompt_field = top_level["userPromptTemplate"]
    input_schema_field = top_level["userInputSchema"]
    prompt_enhancement_field = top_level["hiddenPromptEnhancement"]
    description_field = top_level["userDescription"]
    schema = _load_json(GALLERY_SCHEMA_PATH)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(draft), key=lambda item: list(item.path)
    )
    input_ids = {item["id"] for item in draft[input_schema_field]}
    referenced_ids = set(PLACEHOLDER.findall(draft[prompt_field]))
    missing_placeholders = sorted(input_ids - referenced_ids)
    unknown_placeholders = sorted(referenced_ids - input_ids)
    missing_free_editable_content = sorted(
        value for value in editable.get("freeEditableContent", []) if value not in draft[prompt_field]
    )
    default_values = {slot["id"]: slot["defaultValue"] for slot in editable["slots"]}
    resolved_prompts = [("defaults", _resolve_prompt(draft[prompt_field], default_values))]
    for slot in editable["slots"]:
        for suggestion in slot.get("suggestions", []):
            scenario_values = {**default_values, slot["id"]: suggestion}
            resolved_prompts.append(
                (f"{slot['id']}={suggestion}", _resolve_prompt(draft[prompt_field], scenario_values))
            )
    unnatural_resolved_prompts = sorted(
        label
        for label, resolved in resolved_prompts
        if PLACEHOLDER.search(resolved) or len(resolved.strip()) < 12 or SENTENCE_PUNCTUATION.search(resolved) is None
    )
    source_leaks = sorted(
        claim
        for claim in source_analysis.get("forbiddenLegacyClaims", [])
        if any(claim in text for text in _deep_strings(draft))
    )
    forbidden_keys = sorted(
        _deep_keys(draft) & set(rules["formalProjection"]["forbiddenKeys"].values())
    )
    forbidden_values = _forbidden_formal_values(draft, rules)
    production_terms = sorted(
        term
        for term in rules["prompt"]["forbiddenProductionTerms"]
        if any(term in text for text in _deep_strings(draft))
    )
    slot_values = {
        value
        for slot in editable["slots"]
        for value in [slot.get("defaultValue", ""), *slot.get("suggestions", [])]
        if value
    }
    free_editable_values = {value for value in editable.get("freeEditableContent", []) if value}
    hidden_text = " ".join(_deep_strings(draft[prompt_enhancement_field]))
    open_content_conflicts = sorted(
        value for value in slot_values | free_editable_values if value in hidden_text
    )
    open_axis_conflicts = sorted(
        token
        for slot in editable["slots"]
        for token in slot.get("hiddenConflictTokens", [])
        if token in hidden_text
    )
    title_slot_leaks = sorted(value for value in slot_values if value in draft[title_field])
    title_forbidden_tokens = sorted(
        token
        for slot in editable["slots"]
        for token in slot.get("titleForbiddenTokens", [])
        if token in draft[title_field]
    )
    identity_contract = rules["identityReplacementContract"]
    identity_plan_fields = identity_contract["planFields"]
    planned_identity_terms = plan.get(identity_plan_fields["neutralityTerms"], [])
    primary_subject_role = rules["slotCompilationContract"]["semanticRoles"]["primarySubject"]
    subject_upload_type = rules["slotCompilationContract"]["slotTypes"][
        "primarySubjectUpload"
    ]
    subject_slot_ids = {
        slot["id"]
        for slot in editable["slots"]
        if slot.get("type") == subject_upload_type
    }
    identity_terms = planned_identity_terms if subject_slot_ids else []
    text_contract = rules["visibleTextContract"]
    text_region_fields = text_contract["regionFields"]
    text_evidence_fields = text_contract["exactEvidenceFields"]
    identity_text_regions = [
        region
        for region in editable[text_contract["analysisFields"]["regions"]]
        if region.get(text_region_fields["valueClass"])
        == text_contract["valueClasses"]["identityRelated"]
    ]
    identity_neutrality_applicable = bool(
        subject_slot_ids and (identity_terms or identity_text_regions)
    )
    non_identity_prompt_content = PLACEHOLDER_WITH_DEFAULT.sub(
        lambda match: "" if match.group(1) in subject_slot_ids else match.group(0),
        draft[prompt_field],
    )
    identity_neutrality_texts = [
        draft[title_field],
        draft[description_field],
        non_identity_prompt_content,
        *_deep_strings(draft[prompt_enhancement_field]),
        *editable.get("freeEditableContent", []),
        *[
            value
            for slot in editable["slots"]
            if slot.get("semanticRole") != primary_subject_role
            for value in [
                slot.get("label", ""),
                slot.get("placeholder", ""),
                slot.get("defaultValue", ""),
                *slot.get("suggestions", []),
            ]
            if isinstance(value, str)
        ],
    ]
    identity_neutrality_leaks = sorted(
        term
        for term in identity_terms
        if isinstance(term, str)
        and term
        and any(term in text for text in identity_neutrality_texts)
    )
    audited_content_sha = _sha_bytes(_canonical_bytes(_semantic_audit_payload(draft, editable, rules)))
    semantic_audit_roles = rules["semanticAuditChecks"]
    semantic_audit_requirements = tuple(semantic_audit_roles.values())
    required_check_fields = {contract["check"] for contract in semantic_audit_requirements}
    required_evidence_fields = {contract["evidence"] for contract in semantic_audit_requirements}
    semantic_checks_payload = semantic_audit.get("checks")
    semantic_evidence_payload = semantic_audit.get("evidence")
    evidence = semantic_evidence_payload if isinstance(semantic_evidence_payload, dict) else {}

    def unique_nonempty_strings(value: Any) -> bool:
        return bool(
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item.strip() for item in value)
            and len(value) == len(set(value))
        )

    resolved_cases_field = semantic_audit_roles["resolvedPrompts"]["evidence"]
    open_axes_field = semantic_audit_roles["openAxes"]["evidence"]
    maximum_difference_field = semantic_audit_roles["maximumDifference"]["evidence"]
    suggestion_reviews_field = semantic_audit_roles["slotSuggestions"]["evidence"]
    instruction_scope_field = semantic_audit_roles["instructionScope"]["evidence"]
    hidden_responsibility_field = semantic_audit_roles["hiddenLayerResponsibilities"]["evidence"]
    identity_neutrality_field = semantic_audit_roles["identityNeutrality"]["evidence"]
    visible_text_classification_field = semantic_audit_roles["visibleTextClassification"]["evidence"]
    resolved_cases = evidence.get(resolved_cases_field)
    reviewed_open_axes = evidence.get(open_axes_field)
    maximum_difference_inputs = evidence.get(maximum_difference_field)
    suggestion_reviews = evidence.get(suggestion_reviews_field)
    instruction_scope_review = evidence.get(instruction_scope_field)
    hidden_responsibility_review = evidence.get(hidden_responsibility_field)
    identity_neutrality_review = evidence.get(identity_neutrality_field)
    visible_text_classification_review = evidence.get(visible_text_classification_field)
    identity_neutrality_fields = identity_contract["neutralityAuditFields"]
    text_audit_fields = text_contract["semanticAuditFields"]
    text_decision_fields = text_contract["semanticDecisionFields"]
    expected_text_decisions = {
        (
            region[text_region_fields["identity"]],
            region[text_region_fields["role"]],
            region[text_region_fields["action"]],
            region[text_region_fields["valueClass"]],
            region[text_region_fields["exactTextEvidence"]][
                text_evidence_fields["language"]
            ],
            tuple(
                region[text_region_fields["exactTextEvidence"]][
                    text_evidence_fields["tokens"]
                ]
            ),
        )
        for region in editable[text_contract["analysisFields"]["regions"]]
    }
    observed_text_decisions = set()
    if isinstance(visible_text_classification_review, dict):
        raw_text_decisions = visible_text_classification_review.get(
            text_audit_fields["decisions"]
        )
        for decision in raw_text_decisions if isinstance(raw_text_decisions, list) else []:
            if isinstance(decision, dict):
                observed_decision = (
                    decision.get(text_region_fields["identity"]),
                    decision.get(text_region_fields["role"]),
                    decision.get(text_region_fields["action"]),
                    decision.get(text_region_fields["valueClass"]),
                    decision.get(text_decision_fields["observedLanguage"]),
                    tuple(decision.get(text_decision_fields["observedTokens"], []))
                    if isinstance(
                        decision.get(text_decision_fields["observedTokens"]), list
                    )
                    and all(
                        isinstance(value, str)
                        for value in decision[text_decision_fields["observedTokens"]]
                    )
                    else None,
                )
                if all(isinstance(value, str) for value in observed_decision[:5]) and isinstance(
                    observed_decision[5], tuple
                ):
                    observed_text_decisions.add(observed_decision)
    identity_neutral_region_ids = {
        region[text_region_fields["identity"]]
        for region in identity_text_regions
    }
    slot_origin_fields = text_contract["slotOriginFields"]
    slot_origin_decisions = (
        visible_text_classification_review.get(text_audit_fields["slotOrigins"])
        if isinstance(visible_text_classification_review, dict)
        else None
    )
    expected_slot_ids = {slot["id"] for slot in editable["slots"]}
    region_by_id = {
        region[text_region_fields["identity"]]: region
        for region in editable[text_contract["analysisFields"]["regions"]]
    }

    def required_slot_origin(slot_id: str) -> str | None:
        return next(
            (
                region[text_region_fields["identity"]]
                for region in region_by_id.values()
                if region.get(text_region_fields["action"])
                == text_contract["actions"]["openSlot"]
                and region.get(text_region_fields["slotIdentity"]) == slot_id
            ),
            None,
        )

    slot_origin_evidence_valid = bool(
        isinstance(slot_origin_decisions, list)
        and len(slot_origin_decisions) == len(expected_slot_ids)
        and all(
            isinstance(decision, dict)
            and set(decision) == set(slot_origin_fields.values())
            and isinstance(decision.get(slot_origin_fields["slotIdentity"]), str)
            and (
                decision.get(slot_origin_fields["originRegionIdentity"]) is None
                or isinstance(
                    decision.get(slot_origin_fields["originRegionIdentity"]), str
                )
            )
            and isinstance(decision.get(slot_origin_fields["explanation"]), str)
            and decision[slot_origin_fields["explanation"]].strip()
            for decision in slot_origin_decisions
        )
        and {
            decision[slot_origin_fields["slotIdentity"]]
            for decision in slot_origin_decisions
        }
        == expected_slot_ids
        and all(
            (
                required_slot_origin(decision[slot_origin_fields["slotIdentity"]])
                is None
                or decision[slot_origin_fields["originRegionIdentity"]]
                == required_slot_origin(decision[slot_origin_fields["slotIdentity"]])
            )
            and (
                decision[slot_origin_fields["originRegionIdentity"]] is None
                or (
                    decision[slot_origin_fields["originRegionIdentity"]] in region_by_id
                    and region_by_id[
                        decision[slot_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["action"])
                    == text_contract["actions"]["openSlot"]
                    and region_by_id[
                        decision[slot_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["slotIdentity"])
                    == decision[slot_origin_fields["slotIdentity"]]
                )
            )
            for decision in slot_origin_decisions
        )
    )
    free_origin_fields = text_contract["freeContentOriginFields"]
    free_origin_decisions = (
        visible_text_classification_review.get(text_audit_fields["freeContentOrigins"])
        if isinstance(visible_text_classification_review, dict)
        else None
    )
    expected_free_content = editable.get("freeEditableContent", [])

    def required_free_content_origin(value: str) -> str | None:
        return next(
            (
                region[text_region_fields["identity"]]
                for region in region_by_id.values()
                if region.get(text_region_fields["action"])
                == text_contract["actions"]["freeEditable"]
                and region.get(text_region_fields["selectedText"]) == value
            ),
            None,
        )

    free_origin_evidence_valid = bool(
        isinstance(free_origin_decisions, list)
        and len(free_origin_decisions) == len(expected_free_content)
        and all(
            isinstance(decision, dict)
            and set(decision) == set(free_origin_fields.values())
            and isinstance(decision.get(free_origin_fields["content"]), str)
            and (
                decision.get(free_origin_fields["originRegionIdentity"]) is None
                or isinstance(
                    decision.get(free_origin_fields["originRegionIdentity"]), str
                )
            )
            and isinstance(decision.get(free_origin_fields["explanation"]), str)
            and decision[free_origin_fields["explanation"]].strip()
            for decision in free_origin_decisions
        )
        and sorted(
            decision[free_origin_fields["content"]]
            for decision in free_origin_decisions
        )
        == sorted(expected_free_content)
        and all(
            (
                required_free_content_origin(decision[free_origin_fields["content"]])
                is None
                or decision[free_origin_fields["originRegionIdentity"]]
                == required_free_content_origin(decision[free_origin_fields["content"]])
            )
            and (
                decision[free_origin_fields["originRegionIdentity"]] is None
                or (
                    decision[free_origin_fields["originRegionIdentity"]] in region_by_id
                    and region_by_id[
                        decision[free_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["action"])
                    == text_contract["actions"]["freeEditable"]
                    and region_by_id[
                        decision[free_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["selectedText"])
                    == decision[free_origin_fields["content"]]
                )
            )
            for decision in free_origin_decisions
        )
    )
    fixed_region_leaks = (
        visible_text_classification_review.get(text_audit_fields["fixedRegionLeaks"])
        if isinstance(visible_text_classification_review, dict)
        else None
    )
    fixed_region_leak_evidence_valid = bool(
        isinstance(fixed_region_leaks, list)
        and all(
            isinstance(region_id, str) and region_id.strip()
            for region_id in fixed_region_leaks
        )
        and len(fixed_region_leaks) == len(set(fixed_region_leaks))
        and not fixed_region_leaks
    )
    expected_resolved_cases = {label for label, _ in resolved_prompts}
    expected_open_axes = {slot["semanticRole"] for slot in editable["slots"]}
    maximum_difference_set = (
        set(maximum_difference_inputs) if unique_nonempty_strings(maximum_difference_inputs) else set()
    )
    prompt_rules = rules["prompt"]
    hidden_roles = prompt_rules["hiddenLayerRoles"]
    semantic_evidence_valid = bool(
        unique_nonempty_strings(resolved_cases)
        and set(resolved_cases) == expected_resolved_cases
        and unique_nonempty_strings(reviewed_open_axes)
        and set(reviewed_open_axes) == expected_open_axes
        and unique_nonempty_strings(maximum_difference_inputs)
        and all(
            maximum_difference_set & set(slot["suggestions"])
            for slot in editable["slots"]
        )
        and unique_nonempty_strings(suggestion_reviews)
        and set(suggestion_reviews) == expected_slot_ids
        and isinstance(instruction_scope_review, dict)
        and set(instruction_scope_review) == {"allowedSections", "outOfScopeContentDetected", "evidence"}
        and unique_nonempty_strings(instruction_scope_review.get("allowedSections"))
        and set(instruction_scope_review["allowedSections"])
        == set(prompt_rules["instructionAllowedSections"].values())
        and instruction_scope_review.get("outOfScopeContentDetected") is False
        and isinstance(instruction_scope_review.get("evidence"), str)
        and instruction_scope_review["evidence"].strip()
        and isinstance(hidden_responsibility_review, dict)
        and set(hidden_responsibility_review)
        == {"lockedConstraintsRole", "preserveRole", "overlapDetected", "evidence"}
        and hidden_responsibility_review.get("lockedConstraintsRole")
        == hidden_roles["lockedConstraints"]
        and hidden_responsibility_review.get("preserveRole") == hidden_roles["preserve"]
        and hidden_responsibility_review.get("overlapDetected") is False
        and isinstance(hidden_responsibility_review.get("evidence"), str)
        and hidden_responsibility_review["evidence"].strip()
        and isinstance(identity_neutrality_review, dict)
        and set(identity_neutrality_review) == set(identity_neutrality_fields.values())
        and identity_neutrality_review.get(identity_neutrality_fields["applicability"])
        is identity_neutrality_applicable
        and identity_neutrality_review.get(
            identity_neutrality_fields["specificIdentityDetected"]
        )
        is False
        and isinstance(
            identity_neutrality_review.get(identity_neutrality_fields["explanation"]), str
        )
        and identity_neutrality_review[identity_neutrality_fields["explanation"]].strip()
        and isinstance(visible_text_classification_review, dict)
        and set(visible_text_classification_review) == set(text_audit_fields.values())
        and isinstance(
            visible_text_classification_review.get(text_audit_fields["reviewedRegionIdentities"]),
            list,
        )
        and all(
            isinstance(region_id, str) and region_id.strip()
            for region_id in visible_text_classification_review[
                text_audit_fields["reviewedRegionIdentities"]
            ]
        )
        and len(
            visible_text_classification_review[
                text_audit_fields["reviewedRegionIdentities"]
            ]
        )
        == len(
            set(
                visible_text_classification_review[
                    text_audit_fields["reviewedRegionIdentities"]
                ]
            )
        )
        and set(
            visible_text_classification_review[text_audit_fields["reviewedRegionIdentities"]]
        )
        == {
            region[text_region_fields["identity"]]
            for region in editable[text_contract["analysisFields"]["regions"]]
        }
        and isinstance(
            visible_text_classification_review.get(text_audit_fields["decisions"]), list
        )
        and len(visible_text_classification_review[text_audit_fields["decisions"]])
        == len(expected_text_decisions)
        and observed_text_decisions == expected_text_decisions
        and slot_origin_evidence_valid
        and free_origin_evidence_valid
        and fixed_region_leak_evidence_valid
        and all(
            isinstance(decision, dict)
            and set(decision)
            == {
                text_region_fields["identity"],
                text_region_fields["role"],
                text_region_fields["action"],
                text_region_fields["valueClass"],
                text_decision_fields["observedLanguage"],
                text_decision_fields["observedTokens"],
                text_decision_fields["identityNeutral"],
                text_audit_fields["explanation"],
            }
            and isinstance(
                decision.get(text_decision_fields["identityNeutral"]), bool
            )
            and (
                not identity_neutrality_applicable
                or decision.get(text_region_fields["identity"])
                not in identity_neutral_region_ids
                or decision.get(text_decision_fields["identityNeutral"]) is True
            )
            and isinstance(decision.get(text_audit_fields["explanation"]), str)
            and decision[text_audit_fields["explanation"]].strip()
            for decision in visible_text_classification_review[text_audit_fields["decisions"]]
        )
        and visible_text_classification_review.get(text_audit_fields["complete"]) is True
        and isinstance(
            visible_text_classification_review.get(text_audit_fields["explanation"]), str
        )
        and visible_text_classification_review[text_audit_fields["explanation"]].strip()
    )

    semantic_audit_contract_valid = (
        semantic_audit.get("artifactType") == "semantic-audit"
        and semantic_audit.get("schemaVersion") == rules["schemaVersion"]
        and isinstance(semantic_checks_payload, dict)
        and set(semantic_checks_payload) == required_check_fields
        and isinstance(semantic_evidence_payload, dict)
        and set(semantic_evidence_payload) == required_evidence_fields
        and semantic_evidence_valid
    )
    semantic_audit_bound = (
        semantic_audit_contract_valid
        and semantic_audit.get("contentSha256") == audited_content_sha
        and semantic_audit.get("observedContentSha256") == audited_content_sha
    )
    semantic_audit_checks = {
        contract["check"]: semantic_audit.get("checks", {}).get(contract["check"]) is True
        for contract in semantic_audit_requirements
    }
    semantic_audit_passed = semantic_audit_bound and all(semantic_audit_checks.values())
    visual_evidence_fields = rules["visualReviewContract"]["evidenceFieldRoles"]
    layers = {
        "schema": {"pass": not errors, "evidence": [error.message for error in errors]},
        "semantic": {
            "pass": not source_leaks
            and not missing_placeholders
            and not unknown_placeholders
            and not missing_free_editable_content
            and not unnatural_resolved_prompts
            and not title_slot_leaks
            and not title_forbidden_tokens
            and not identity_neutrality_leaks
            and semantic_audit_passed,
            "evidence": {
                "sourceLeaks": source_leaks,
                "missingSlotBindings": missing_placeholders,
                "unknownSlotBindings": unknown_placeholders,
                "missingFreeEditableContent": missing_free_editable_content,
                "unnaturalResolvedPrompts": unnatural_resolved_prompts,
                "titleSlotLeaks": title_slot_leaks,
                "titleForbiddenTokens": title_forbidden_tokens,
                "identityNeutralityLeaks": identity_neutrality_leaks,
                "semanticAudit": {
                    "contractValid": semantic_audit_contract_valid,
                    "contentBound": semantic_audit_bound,
                    "checks": semantic_audit_checks,
                    "evidence": semantic_audit.get("evidence", {}),
                },
            },
        },
        "visualContract": {
            "pass": all(review[visual_evidence_fields["hardGates"]].values())
            and all(
                item["pass"]
                for item in review[visual_evidence_fields["visualDimensions"]].values()
            ),
            "evidence": {"reviewSha256": _sha_bytes(_json_bytes(review))},
        },
        "galleryContract": {
            "pass": not forbidden_keys
            and not forbidden_values
            and not production_terms
            and not open_content_conflicts
            and not open_axis_conflicts,
            "evidence": {
                "forbiddenKeys": forbidden_keys,
                "forbiddenValues": forbidden_values,
                "productionTerms": production_terms,
                "openContentConflicts": open_content_conflicts,
                "openAxisConflicts": open_axis_conflicts,
            },
        },
    }
    return {
        "artifactType": "validation-report",
        "schemaVersion": rules["schemaVersion"],
        "layers": layers,
        "pass": all(layer["pass"] for layer in layers.values()),
    }


def _formal_projection(draft: dict[str, Any], url: str, rules: dict[str, Any]) -> dict[str, Any]:
    contract = rules["formalProjection"]
    top_level = contract["topLevel"]
    metadata_field = top_level["formalMetadata"]
    cover_field = top_level["coverAsset"]
    reference_field = top_level["referenceAsset"]
    review_field = contract["metadata"]["reviewReason"]
    allowed_top_level = set(contract["topLevel"].values())
    unexpected_top_level = sorted(set(draft) - allowed_top_level)
    metadata = draft.get(metadata_field)
    allowed_metadata = set(contract["metadata"].values())
    recognized_sidecars = set(contract["recognizedMetadataSidecars"].values())
    unexpected_metadata = sorted(
        set(metadata) - allowed_metadata - recognized_sidecars
        if isinstance(metadata, dict)
        else []
    )
    needs_review = metadata.get(review_field) if isinstance(metadata, dict) else None
    source_valid = bool(
        not unexpected_top_level
        and isinstance(metadata, dict)
        and not unexpected_metadata
        and (
            review_field not in metadata
            or (isinstance(needs_review, str) and needs_review.strip())
        )
        and _public_asset_url_valid(url, rules)
    )
    if not source_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "正式投影源包含未知字段、无效复核原因或非 HTTPS 模板图 URL。",
            {
                "unexpectedTopLevel": unexpected_top_level,
                "unexpectedMetadata": unexpected_metadata,
            },
        )
    complete = copy.deepcopy(draft)
    complete[cover_field] = url
    complete[reference_field] = url
    projection = {
        key: complete[key]
        for key in contract["topLevel"].values()
        if key in complete
    }
    projection[metadata_field] = {
        key: complete[metadata_field][key]
        for key in contract["metadata"].values()
        if key in complete[metadata_field]
    }
    return projection


def _validate_final(record: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    schema = _load_json(GALLERY_SCHEMA_PATH)
    contract = rules["formalProjection"]
    top_level = contract["topLevel"]
    metadata_field = top_level["formalMetadata"]
    status_field = top_level["lifecycleStatus"]
    cover_field = top_level["coverAsset"]
    reference_field = top_level["referenceAsset"]
    review_field = contract["metadata"]["reviewReason"]
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(record), key=lambda item: list(item.path)
    )
    forbidden_keys = sorted(_deep_keys(record) & set(contract["forbiddenKeys"].values()))
    expected_top_level = set(contract["topLevel"].values())
    top_level_extra = sorted(set(record) - expected_top_level)
    top_level_missing = sorted(expected_top_level - set(record))
    metadata = record.get(metadata_field)
    metadata_extra = sorted(
        set(metadata) - set(contract["metadata"].values())
        if isinstance(metadata, dict)
        else []
    )
    forbidden_values = _forbidden_formal_values(record, rules)
    production_terms = sorted(
        term
        for term in rules["prompt"]["forbiddenProductionTerms"]
        if any(term in value for value in _deep_strings(record))
    )
    needs_review = metadata.get(review_field) if isinstance(metadata, dict) else None
    needs_review_valid = bool(
        isinstance(metadata, dict)
        and (
            review_field not in metadata
            or (
                isinstance(needs_review, str)
                and needs_review.strip()
                and record.get(status_field) == contract["statusValues"]["draft"]
            )
        )
    )
    cover = record.get(cover_field)
    reference_image = record.get(reference_field)
    cover_matches_reference = cover == reference_image
    asset_urls_valid = bool(
        _public_asset_url_valid(cover, rules)
        and _public_asset_url_valid(reference_image, rules)
    )
    passed = bool(
        not errors
        and not forbidden_keys
        and not top_level_extra
        and not top_level_missing
        and not metadata_extra
        and not forbidden_values
        and not production_terms
        and needs_review_valid
        and cover_matches_reference
        and asset_urls_valid
    )
    return {
        "artifactType": "final-validation-report",
        "schemaVersion": rules["schemaVersion"],
        "pass": passed,
        "schemaErrors": [error.message for error in errors],
        "forbiddenKeys": forbidden_keys,
        "topLevelExtra": top_level_extra,
        "topLevelMissing": top_level_missing,
        "metadataExtra": metadata_extra,
        "forbiddenValues": forbidden_values,
        "productionTerms": production_terms,
        "needsReviewValid": needs_review_valid,
        "coverMatchesReferenceImage": cover_matches_reference,
        "assetUrlsValid": asset_urls_valid,
    }


def formal_template_contract_valid(
    record: Any, rules: dict[str, Any]
) -> bool:
    """Return whether a persisted Gallery template satisfies the formal contract."""
    if not isinstance(record, dict):
        return False
    return bool(_validate_final(record, rules)["pass"])
