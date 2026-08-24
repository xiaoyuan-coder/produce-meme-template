from __future__ import annotations

import copy
from typing import Any

from .artifacts import canonical_json_bytes, sha256_bytes


def _digest(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


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
