from __future__ import annotations

import copy
from typing import Any

from .artifacts import canonical_json_bytes, sha256_bytes


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def default_subject_binding_analysis(
    source_analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    """Build the conservative fixture baseline; live analyzers author bound groups."""

    context = rules["sourceAuthoringContextContract"]
    fields = context["subjectBindingFields"]
    group_fields = context["subjectBindingGroupFields"]
    relationships = context["subjectBindingRelationships"]
    multi = rules["multiInstanceContract"]
    graph = source_analysis[multi["sourceFields"]["componentGraph"]]
    components = graph[multi["graphFields"]["components"]]
    component_fields = multi["componentFields"]
    identity_units = sorted(
        {
            component[component_fields["identityUnit"]]
            for component in components
            if isinstance(component.get(component_fields["identityUnit"]), str)
            and component[component_fields["identityUnit"]].strip()
        }
    )
    groups = []
    for index, identity_unit in enumerate(identity_units):
        required = sorted(
            component[component_fields["identity"]]
            for component in components
            if component.get(component_fields["identityUnit"]) == identity_unit
        )
        visible_instances = sum(
            component.get(component_fields["visualInstance"]) is True
            for component in components
            if component.get(component_fields["identityUnit"]) == identity_unit
        )
        groups.append(
            {
                group_fields["identity"]: f"subject-binding-{index + 1}",
                group_fields["relationship"]: (
                    relationships["repeatedIdentity"]
                    if visible_instances > 1
                    else relationships["independent"]
                ),
                group_fields["identityUnits"]: [identity_unit],
                group_fields["requiredComponents"]: required,
                group_fields["evidence"]: (
                    "fixture 按身份单元覆盖主体、专属配饰、文字、阴影、倒影和重复实例"
                ),
            }
        )
    return {
        fields["assessed"]: True,
        fields["groups"]: groups,
        fields["evidence"]: "逐个身份单元完成绑定组与随身份变化组件核对",
    }


def _subject_edit_intent(
    source_analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    contract = rules["authoringHandoffContract"]["subjectEditIntentContract"]
    fields = contract["fields"]
    modes = contract["bindingModes"]
    multi = rules["multiInstanceContract"]
    graph = source_analysis["componentGraph"]
    components = graph[multi["graphFields"]["components"]]
    relations = graph[multi["graphFields"]["relations"]]
    component_fields = multi["componentFields"]
    relation_fields = multi["relationFields"]
    repeated_identity = multi["relationTypes"]["repeatedIdentity"]
    subject_components = [
        component
        for component in components
        if component[component_fields["visualInstance"]] is True
        and component[component_fields["identityUnit"]] is not None
    ]
    identity_units = sorted(
        {
            component[component_fields["identityUnit"]]
            for component in subject_components
        }
    )
    subject_count = len(identity_units)
    binding_mode = (
        modes["none"]
        if subject_count == 0
        else modes["single"]
        if subject_count == 1
        else modes["multiple"]
    )
    return {
        fields["identityUnits"]: identity_units,
        fields["subjectComponents"]: sorted(
            component[component_fields["identity"]]
            for component in subject_components
        ),
        fields["repeatedIdentityRelations"]: sorted(
            relation[relation_fields["identity"]]
            for relation in relations
            if relation[relation_fields["type"]] == repeated_identity
        ),
        fields["subjectCount"]: subject_count,
        fields["bindingMode"]: binding_mode,
    }


def _subject_edit_intent_valid(value: Any, rules: dict[str, Any]) -> bool:
    contract = rules["authoringHandoffContract"]["subjectEditIntentContract"]
    fields = contract["fields"]
    return bool(
        isinstance(value, dict)
        and set(value) == set(fields.values())
        and all(
            isinstance(value.get(fields[role]), list)
            and all(
                isinstance(item, str) and item.strip()
                for item in value[fields[role]]
            )
            and len(value[fields[role]]) == len(set(value[fields[role]]))
            for role in (
                "identityUnits",
                "subjectComponents",
                "repeatedIdentityRelations",
            )
        )
        and isinstance(value.get(fields["subjectCount"]), int)
        and not isinstance(value[fields["subjectCount"]], bool)
        and value[fields["subjectCount"]]
        == len(value[fields["identityUnits"]])
        and value.get(fields["bindingMode"])
        in set(contract["bindingModes"].values())
    )


def source_authoring_context_errors(
    source_analysis: dict[str, Any], rules: dict[str, Any]
) -> list[str]:
    """Validate the P1 facts that protect IP, identity, and meme continuity."""

    contract = rules["sourceAuthoringContextContract"]
    cultural = source_analysis.get(contract["culturalReferenceField"])
    cultural_fields = contract["culturalReferenceFields"]
    statuses = contract["culturalReferenceStatuses"]
    reference_fields = contract["referenceFields"]
    errors: list[str] = []
    cultural_valid = bool(
        isinstance(cultural, dict)
        and set(cultural) == set(cultural_fields.values())
        and cultural.get(cultural_fields["assessed"]) is True
        and cultural.get(cultural_fields["status"]) in set(statuses.values())
        and isinstance(cultural.get(cultural_fields["checkedSignals"]), list)
        and cultural[cultural_fields["checkedSignals"]]
        and all(
            isinstance(value, str) and value.strip()
            for value in cultural[cultural_fields["checkedSignals"]]
        )
        and isinstance(cultural.get(cultural_fields["references"]), list)
        and all(
            isinstance(reference, dict)
            and set(reference) == set(reference_fields.values())
            and all(
                isinstance(reference.get(field), str)
                and reference[field].strip()
                for field in reference_fields.values()
            )
            for reference in cultural[cultural_fields["references"]]
        )
        and isinstance(cultural.get(cultural_fields["candidates"]), list)
        and all(
            isinstance(value, str) and value.strip()
            for value in cultural[cultural_fields["candidates"]]
        )
        and isinstance(cultural.get(cultural_fields["evidence"]), str)
        and cultural[cultural_fields["evidence"]].strip()
    )
    if cultural_valid:
        status = cultural[cultural_fields["status"]]
        references = cultural[cultural_fields["references"]]
        candidates = cultural[cultural_fields["candidates"]]
        cultural_valid = bool(
            (status == statuses["identified"] and references)
            or (status == statuses["notDetected"] and not references and not candidates)
            or (status == statuses["uncertain"] and candidates)
        )
    if not cultural_valid:
        errors.append("culturalReferenceDiscovery 缺少强制 IP/文化身份发现结论")

    continuity = source_analysis.get(contract["subjectContinuityField"])
    continuity_fields = contract["subjectContinuityFields"]
    continuity_valid = bool(
        isinstance(continuity, dict)
        and set(continuity) == set(continuity_fields.values())
        and isinstance(continuity.get(continuity_fields["subjectCount"]), int)
        and not isinstance(continuity[continuity_fields["subjectCount"]], bool)
        and continuity[continuity_fields["subjectCount"]] >= 0
        and all(
            isinstance(continuity.get(field), str)
            and continuity[field].strip()
            for role, field in continuity_fields.items()
            if role not in {"subjectCount", "preserveTraits"}
        )
        and isinstance(continuity.get(continuity_fields["preserveTraits"]), list)
        and continuity[continuity_fields["preserveTraits"]]
        and all(
            isinstance(value, str) and value.strip()
            for value in continuity[continuity_fields["preserveTraits"]]
        )
    )
    if not continuity_valid:
        errors.append("subjectContinuity 缺少人数、性别/年龄、物种、服装角色或反差机制证据")

    canvas_contract = rules["sourceCanvasContract"]
    canvas = source_analysis.get(canvas_contract["field"])
    canvas_fields = canvas_contract["fields"]
    modes = canvas_contract["modes"]
    actions = canvas_contract["actions"]
    required_actions = {
        modes[mode_role]: {
            actions[action_role]
            for action_role in action_roles
        }
        for mode_role, action_roles in canvas_contract[
            "requiredActionsByMode"
        ].items()
    }
    canvas_mode = canvas.get(canvas_fields["mode"]) if isinstance(canvas, dict) else None
    canvas_valid = bool(
        isinstance(canvas, dict)
        and set(canvas) == set(canvas_fields.values())
        and canvas_mode in set(modes.values())
        and isinstance(canvas.get(canvas_fields["targetRegions"]), list)
        and canvas[canvas_fields["targetRegions"]]
        and all(
            isinstance(value, str) and value.strip()
            for value in canvas[canvas_fields["targetRegions"]]
        )
        and len(canvas[canvas_fields["targetRegions"]])
        == len(set(canvas[canvas_fields["targetRegions"]]))
        and isinstance(canvas.get(canvas_fields["excludedCarrierRegions"]), list)
        and all(
            isinstance(value, str) and value.strip()
            for value in canvas[canvas_fields["excludedCarrierRegions"]]
        )
        and len(canvas[canvas_fields["excludedCarrierRegions"]])
        == len(set(canvas[canvas_fields["excludedCarrierRegions"]]))
        and set(canvas.get(canvas_fields["requiredActions"], []))
        == required_actions.get(canvas_mode)
        and isinstance(canvas.get(canvas_fields["preserveDesignFeatures"]), list)
        and canvas[canvas_fields["preserveDesignFeatures"]]
        and all(
            isinstance(value, str) and value.strip()
            for value in canvas[canvas_fields["preserveDesignFeatures"]]
        )
        and isinstance(canvas.get(canvas_fields["evidence"]), str)
        and canvas[canvas_fields["evidence"]].strip()
        and (
            canvas_mode not in {modes["printArtwork"], modes["screenContent"]}
            or bool(canvas[canvas_fields["excludedCarrierRegions"]])
        )
    )
    if not canvas_valid:
        errors.append("sourceCanvasDecision 缺少载体/截图目标画布路由或必需裁切动作")

    mark_contract = rules["sourceMarkTreatmentContract"]
    mark_policy = source_analysis.get(mark_contract["field"])
    policy_fields = mark_contract["fields"]
    treatment_fields = mark_contract["treatmentFields"]
    types = mark_contract["types"]
    mark_actions = mark_contract["actions"]
    bases = mark_contract["bases"]
    treatments = (
        mark_policy.get(policy_fields["treatments"])
        if isinstance(mark_policy, dict)
        else None
    )
    mark_policy_valid = bool(
        isinstance(mark_policy, dict)
        and set(mark_policy) == set(policy_fields.values())
        and mark_policy.get(policy_fields["assessed"]) is True
        and isinstance(treatments, list)
        and isinstance(mark_policy.get(policy_fields["evidence"]), str)
        and mark_policy[policy_fields["evidence"]].strip()
        and all(
            isinstance(item, dict)
            and set(item) == set(treatment_fields.values())
            and all(
                isinstance(item.get(treatment_fields[role]), str)
                and item[treatment_fields[role]].strip()
                for role in treatment_fields
            )
            and item[treatment_fields["type"]] in set(types.values())
            and item[treatment_fields["action"]] in set(mark_actions.values())
            and item[treatment_fields["basis"]] in set(bases.values())
            for item in treatments or []
        )
        and len(
            {
                item[treatment_fields["identity"]]
                for item in treatments or []
                if isinstance(item, dict)
                and isinstance(item.get(treatment_fields["identity"]), str)
            }
        )
        == len(treatments or [])
    )
    if mark_policy_valid:
        remove_only_types = {
            types["watermark"],
            types["platformMark"],
            types["accountMark"],
            types["pseudoSignature"],
            types["unrelatedLogo"],
        }
        preserve_types = {types["sticker"], types["decorativeIcon"]}
        brand_remove_bases = {
            bases["explicitUserRequest"],
            bases["carrierContext"],
            bases["identityDependency"],
            bases["rightsBlocked"],
        }
        mark_policy_valid = all(
            (
                item[treatment_fields["type"]] not in remove_only_types
                or item[treatment_fields["action"]] == mark_actions["remove"]
            )
            and (
                item[treatment_fields["type"]] not in preserve_types
                or item[treatment_fields["action"]]
                in {mark_actions["preserve"], mark_actions["synchronize"]}
            )
            and (
                item[treatment_fields["type"]] != types["brandMark"]
                or item[treatment_fields["action"]] != mark_actions["remove"]
                or item[treatment_fields["basis"]] in brand_remove_bases
            )
            for item in treatments
        )
    if not mark_policy_valid:
        errors.append("sourceMarkPolicy 未逐项区分水印污染与应保留的贴纸/商标")

    visual = source_analysis.get("visualContract")
    visual_fields = contract["sourceVisualContractFields"]
    if not (
        isinstance(visual, dict)
        and set(visual) == set(visual_fields.values())
        and all(
            isinstance(visual.get(field), str) and visual[field].strip()
            for field in visual_fields.values()
        )
    ):
        errors.append("visualContract 必须逐项冻结媒介、形态、边缘、色光、表面与构图")

    binding = source_analysis.get(contract["subjectBindingField"])
    binding_fields = contract["subjectBindingFields"]
    group_fields = contract["subjectBindingGroupFields"]
    relationships = contract["subjectBindingRelationships"]
    multi = rules["multiInstanceContract"]
    graph = source_analysis.get(multi["sourceFields"]["componentGraph"])
    graph_fields = multi["graphFields"]
    component_fields = multi["componentFields"]
    components = (
        graph.get(graph_fields["components"])
        if isinstance(graph, dict)
        else None
    )
    component_by_id = {
        component.get(component_fields["identity"]): component
        for component in components or []
        if isinstance(component, dict)
        and isinstance(component.get(component_fields["identity"]), str)
    }
    observed_identity_units = {
        component[component_fields["identityUnit"]]
        for component in component_by_id.values()
        if isinstance(component.get(component_fields["identityUnit"]), str)
        and component[component_fields["identityUnit"]].strip()
    }
    groups = (
        binding.get(binding_fields["groups"])
        if isinstance(binding, dict)
        else None
    )
    group_shape_valid = bool(
        isinstance(binding, dict)
        and set(binding) == set(binding_fields.values())
        and binding.get(binding_fields["assessed"]) is True
        and isinstance(binding.get(binding_fields["evidence"]), str)
        and binding[binding_fields["evidence"]].strip()
        and isinstance(groups, list)
        and all(
            isinstance(group, dict)
            and set(group) == set(group_fields.values())
            and isinstance(group.get(group_fields["identity"]), str)
            and group[group_fields["identity"]].strip()
            and group.get(group_fields["relationship"])
            in set(relationships.values())
            and isinstance(group.get(group_fields["identityUnits"]), list)
            and group[group_fields["identityUnits"]]
            and all(
                isinstance(value, str) and value.strip()
                for value in group[group_fields["identityUnits"]]
            )
            and len(group[group_fields["identityUnits"]])
            == len(set(group[group_fields["identityUnits"]]))
            and isinstance(group.get(group_fields["requiredComponents"]), list)
            and group[group_fields["requiredComponents"]]
            and all(
                isinstance(value, str) and value in component_by_id
                for value in group[group_fields["requiredComponents"]]
            )
            and len(group[group_fields["requiredComponents"]])
            == len(set(group[group_fields["requiredComponents"]]))
            and isinstance(group.get(group_fields["evidence"]), str)
            and group[group_fields["evidence"]].strip()
            for group in groups or []
        )
    )
    if group_shape_valid:
        group_ids = [group[group_fields["identity"]] for group in groups]
        listed_units = [
            identity_unit
            for group in groups
            for identity_unit in group[group_fields["identityUnits"]]
        ]
        group_shape_valid = bool(
            len(group_ids) == len(set(group_ids))
            and len(listed_units) == len(set(listed_units))
            and set(listed_units) == observed_identity_units
            and all(
                {
                    component_id
                    for component_id, component in component_by_id.items()
                    if component.get(component_fields["identityUnit"])
                    in set(group[group_fields["identityUnits"]])
                }
                <= set(group[group_fields["requiredComponents"]])
                for group in groups
            )
            and all(
                (
                    len(group[group_fields["identityUnits"]]) == 1
                    if group[group_fields["relationship"]]
                    in {
                        relationships["independent"],
                        relationships["repeatedIdentity"],
                    }
                    else len(group[group_fields["identityUnits"]]) == 2
                    if group[group_fields["relationship"]]
                    == relationships["boundPair"]
                    else len(group[group_fields["identityUnits"]]) >= 2
                )
                for group in groups
            )
        )
    if not group_shape_valid:
        errors.append("subjectBindingAnalysis 未完整覆盖主体绑定组及随身份变化组件")
    if (
        continuity_valid
        and continuity[continuity_fields["subjectCount"]]
        != len(observed_identity_units)
    ):
        errors.append("subjectContinuity 主体数与组件图身份单元数不一致")
    return errors


def compile_authoring_intent(
    source_analysis: dict[str, Any],
    replacement_plan: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the reusable P1 meaning needed by both generation and authoring."""

    contract = rules["authoringHandoffContract"]
    subject_edit_contract = contract["subjectEditIntentContract"]
    source_intent = {
        "mechanism": copy.deepcopy(replacement_plan["mechanism"]),
        "target": copy.deepcopy(source_analysis["target"]),
        "componentGraph": copy.deepcopy(source_analysis["componentGraph"]),
        "frozenSet": copy.deepcopy(replacement_plan["frozenSet"]),
        "languagePolicy": source_analysis["languagePolicy"],
        "visualContract": copy.deepcopy(source_analysis["visualContract"]),
        "spatialRelations": copy.deepcopy(source_analysis.get("spatialRelations", [])),
        "culturalReferenceDiscovery": copy.deepcopy(
            source_analysis.get("culturalReferenceDiscovery")
        ),
        "subjectContinuity": copy.deepcopy(source_analysis.get("subjectContinuity")),
        "subjectBindingAnalysis": copy.deepcopy(
            source_analysis.get("subjectBindingAnalysis")
        ),
        rules["sourceCanvasContract"]["field"]: copy.deepcopy(
            source_analysis.get(rules["sourceCanvasContract"]["field"])
        ),
        rules["sourceMarkTreatmentContract"]["field"]: copy.deepcopy(
            source_analysis.get(rules["sourceMarkTreatmentContract"]["field"])
        ),
        subject_edit_contract["field"]: _subject_edit_intent(source_analysis, rules),
    }
    replacement_intent = {
        "primaryTargets": copy.deepcopy(replacement_plan["primaryTargets"]),
        "dependencyClosure": copy.deepcopy(replacement_plan["dependencyClosure"]),
        "changedSet": copy.deepcopy(replacement_plan["changedSet"]),
        "componentGraph": copy.deepcopy(replacement_plan["componentGraph"]),
        "imageOperations": copy.deepcopy(replacement_plan["imageOperations"]),
    }
    for field in (
        "identityRoute",
        "identityTopology",
        "identityTextDecisions",
        "identityNeutralityTerms",
    ):
        if field in replacement_plan:
            replacement_intent[field] = copy.deepcopy(replacement_plan[field])
    return {
        "artifactType": contract["artifactTypes"]["intent"],
        "schemaVersion": rules["schemaVersion"],
        "sourceIntent": source_intent,
        "replacementIntent": replacement_intent,
        "bindings": {
            "sourceImageSha256": source_analysis["sourceImageSha256"],
            "sourceAnalysisSha256": _digest(source_analysis),
            "replacementPlanSha256": _digest(replacement_plan),
        },
    }


def compile_authoring_handoff(
    authoring_intent: dict[str, Any],
    visual_review: dict[str, Any],
    generation_package: dict[str, Any],
    approved_image_sha256: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Resolve P1 intent against the approved P2 pixels for incremental P3 work."""

    contract = rules["authoringHandoffContract"]
    review_contract = rules["visualReviewContract"]
    evidence_fields = review_contract["evidenceFieldRoles"]
    return {
        "artifactType": contract["artifactTypes"]["handoff"],
        "schemaVersion": rules["schemaVersion"],
        "sourceIntent": copy.deepcopy(authoring_intent["sourceIntent"]),
        "replacementIntent": copy.deepcopy(authoring_intent["replacementIntent"]),
        "approvedDelta": {
            "decision": visual_review["decision"],
            "hardGates": copy.deepcopy(
                visual_review.get(evidence_fields["hardGates"], {})
            ),
            "visualDimensions": copy.deepcopy(
                visual_review.get(evidence_fields["visualDimensions"], {})
            ),
            "visibleTextEvidence": copy.deepcopy(
                visual_review.get(evidence_fields["visibleText"])
            ),
            "identityTextEvidence": copy.deepcopy(
                visual_review.get(evidence_fields["identityText"])
            ),
            "imageOperationEvidence": copy.deepcopy(
                visual_review.get(evidence_fields["imageOperations"])
            ),
            "authoringMode": contract["authoringModes"]["incrementalDelta"],
        },
        "bindings": {
            **copy.deepcopy(authoring_intent["bindings"]),
            "generationPackageSha256": _digest(generation_package),
            "authoringIntentSha256": _digest(authoring_intent),
            "visualReviewSha256": _digest(visual_review),
            "approvedImageSha256": approved_image_sha256,
        },
    }


def authoring_handoff_valid(
    handoff: Any,
    approved_image_sha256: str,
    rules: dict[str, Any],
) -> bool:
    contract = rules["authoringHandoffContract"]
    return bool(
        isinstance(handoff, dict)
        and handoff.get("artifactType") == contract["artifactTypes"]["handoff"]
        and handoff.get("schemaVersion") == rules["schemaVersion"]
        and isinstance(handoff.get("sourceIntent"), dict)
        and isinstance(handoff["sourceIntent"].get("mechanism"), dict)
        and _subject_edit_intent_valid(
            handoff["sourceIntent"].get(
                contract["subjectEditIntentContract"]["field"]
            ),
            rules,
        )
        and isinstance(handoff.get("replacementIntent"), dict)
        and isinstance(handoff["replacementIntent"].get("primaryTargets"), list)
        and isinstance(handoff.get("approvedDelta"), dict)
        and handoff["approvedDelta"].get("authoringMode")
        == contract["authoringModes"]["incrementalDelta"]
        and isinstance(handoff.get("bindings"), dict)
        and handoff["bindings"].get("approvedImageSha256")
        == approved_image_sha256
        and all(
            isinstance(handoff["bindings"].get(field), str)
            and len(handoff["bindings"][field]) == 64
            and all(
                character in "0123456789abcdef"
                for character in handoff["bindings"][field]
            )
            for field in contract["requiredDigestFields"]
        )
    )
