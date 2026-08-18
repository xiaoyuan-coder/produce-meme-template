from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json as _load_json,
    sha256_bytes as _sha_bytes,
    sha256_file as _sha_file,
)
from .replacement_planning import _plan_replacement
from .workflow import (
    WorkflowAdapters,
    WorkflowStop,
    _adapter_snapshot_image_object_call,
    _stop,
)


def _replacement_strategy_errors(request: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    if "replacementStrategy" not in request:
        return []
    strategy = request["replacementStrategy"]
    contract = rules["replacementStrategyContract"]
    if not isinstance(strategy, dict):
        return ["replacementStrategy must be an object"]
    errors = [
        f"replacementStrategy.{field} is not allowed"
        for field in sorted(set(strategy) - set(contract["allowedFields"]))
    ]
    for left, right in contract["pairedFields"]:
        if (strategy.get(left) is None) != (strategy.get(right) is None):
            errors.append(f"replacementStrategy.{left} and {right} must be provided together")
    for field in contract["listFields"]:
        if field not in strategy:
            continue
        values = strategy[field]
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value.strip() for value in values)
            or len(values) != len(set(values))
        ):
            errors.append(f"replacementStrategy.{field} must be a non-empty unique string list")
    if not any(strategy.get(field) for field in contract["actionFields"]):
        errors.append("replacementStrategy must declare at least one action")
    category = strategy.get("replacementCategory")
    if category is not None and category not in rules["sourceCategories"].values():
        errors.append("replacementStrategy.replacementCategory is unknown")
    for field in ("policyId", "policyVersion", "replacementValue"):
        if field in strategy and (not isinstance(strategy[field], str) or not strategy[field].strip()):
            errors.append(f"replacementStrategy.{field} must be a non-empty string")
    return errors


def _generation_options_errors(request: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    contract = rules["generationExecutionContract"]
    options_field = contract["requestOptionsField"]
    if options_field not in request:
        return []
    options = request[options_field]
    fields = contract["requestOptionFields"]
    if not isinstance(options, dict):
        return [f"{options_field} must be an object"]
    errors = [
        f"{options_field}.{field} is not allowed"
        for field in sorted(set(options) - set(fields.values()))
    ]
    image_count = options.get(fields["imageCount"], contract["defaultImageCount"])
    primary_index = options.get(
        fields["primaryOutputIndex"], contract["defaultPrimaryOutputIndex"]
    )
    if (
        not isinstance(image_count, int)
        or isinstance(image_count, bool)
        or not 1 <= image_count <= contract["maximumImageCount"]
    ):
        errors.append(
            f"{options_field}.{fields['imageCount']} must be an integer between 1 and "
            f"{contract['maximumImageCount']}"
        )
    if (
        not isinstance(primary_index, int)
        or isinstance(primary_index, bool)
        or not isinstance(image_count, int)
        or isinstance(image_count, bool)
        or not 0 <= primary_index < image_count
    ):
        errors.append(
            f"{options_field}.{fields['primaryOutputIndex']} must select a requested output"
        )
    return errors


def _production_request_errors(
    request: dict[str, Any],
    rules: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    template_key = request.get("templateKey")
    production_item_id = request.get("productionItemId")
    if (
        not isinstance(template_key, str)
        or re.fullmatch(schema["properties"]["key"]["pattern"], template_key)
        is None
    ):
        errors.append("非法标识符：templateKey")
    if production_item_id is not None and (
        not isinstance(production_item_id, str)
        or re.fullmatch(
            rules["identifiers"]["productionItemIdPattern"],
            production_item_id,
        )
        is None
    ):
        errors.append("非法标识符：productionItemId")
    source_image = request.get("sourceImage")
    if not isinstance(source_image, (str, os.PathLike)) or not str(
        source_image
    ).strip():
        errors.append("sourceImage must be a non-empty path")
    errors.extend(_replacement_strategy_errors(request, rules))
    errors.extend(_generation_options_errors(request, rules))
    return errors


def _isolated_output_dir(output_root: Path, item_id: str) -> Path | None:
    lexical_path = output_root / item_id
    if lexical_path.is_symlink() or lexical_path.resolve() != lexical_path:
        return None
    return lexical_path


def _normalized_generation_options(
    request: dict[str, Any], rules: dict[str, Any]
) -> dict[str, int]:
    contract = rules["generationExecutionContract"]
    fields = contract["requestOptionFields"]
    options = request.get(contract["requestOptionsField"], {})
    return {
        fields["imageCount"]: options.get(
            fields["imageCount"], contract["defaultImageCount"]
        ),
        fields["primaryOutputIndex"]: options.get(
            fields["primaryOutputIndex"], contract["defaultPrimaryOutputIndex"]
        ),
    }


def _normalize_replacement_strategy(request: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any] | None:
    strategy = request.get("replacementStrategy")
    if strategy is None:
        return None
    normalized = dict(strategy)
    for field in rules["replacementStrategyContract"]["listFields"]:
        if field in normalized:
            normalized[field] = sorted(normalized[field])
    return normalized


def _shared_policy_errors(
    policy: Any, item_ids: set[str], rules: dict[str, Any]
) -> list[str]:
    contract = rules["batchProductionContract"]
    fields = contract["sharedPolicyFields"]
    pool_fields = contract["replacementPoolEntryFields"]
    required = {
        fields["policyIdentity"],
        fields["policyVersion"],
        fields["policyRevision"],
        fields["scope"],
        fields["replacementPool"],
    }
    allowed = set(fields.values())
    if not isinstance(policy, dict):
        return ["sharedPolicy must be an object"]
    errors = [
        f"sharedPolicy.{field} is not allowed"
        for field in sorted(set(policy) - allowed)
    ]
    if not required <= set(policy):
        errors.append("sharedPolicy is missing required fields")
    for role in ("policyIdentity", "policyVersion", "policyRevision"):
        value = policy.get(fields[role])
        if not isinstance(value, str) or not value.strip():
            errors.append(f"sharedPolicy.{fields[role]} must be a non-empty string")
    scope = policy.get(fields["scope"])
    if (
        not isinstance(scope, list)
        or not scope
        or not all(isinstance(value, str) and value for value in scope)
        or len(scope) != len(set(scope))
        or not set(scope) <= item_ids
    ):
        errors.append("sharedPolicy.scope must identify unique batch items")
    pool = policy.get(fields["replacementPool"])
    categories = set(rules["sourceCategories"].values())
    if (
        not isinstance(pool, list)
        or not pool
        or len(pool) > contract["maximumReplacementPoolItems"]
        or not all(
            isinstance(entry, dict)
            and set(entry) == set(pool_fields.values())
            and isinstance(entry.get(pool_fields["replacementValue"]), str)
            and entry[pool_fields["replacementValue"]].strip()
            and entry.get(pool_fields["replacementCategory"]) in categories
            for entry in pool
        )
        or len(
            {
                (
                    entry[pool_fields["replacementValue"]],
                    entry[pool_fields["replacementCategory"]],
                )
                for entry in pool
                if isinstance(entry, dict)
                and set(entry) == set(pool_fields.values())
            }
        )
        != (len(pool) if isinstance(pool, list) else -1)
    ):
        errors.append(
            "sharedPolicy.replacementPool must contain a bounded set of unique typed values"
        )
    for role in ("preserve", "forbidValues"):
        field = fields[role]
        if field not in policy:
            continue
        values = policy[field]
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value.strip() for value in values)
            or len(values) != len(set(values))
        ):
            errors.append(f"sharedPolicy.{field} must be a non-empty unique string list")
    return errors


def _normalize_shared_policy(
    policy: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    normalized = copy.deepcopy(policy)
    fields = rules["batchProductionContract"]["sharedPolicyFields"]
    pool_fields = rules["batchProductionContract"]["replacementPoolEntryFields"]
    normalized[fields["scope"]] = sorted(normalized[fields["scope"]])
    normalized[fields["replacementPool"]] = sorted(
        normalized[fields["replacementPool"]],
        key=lambda entry: (
            entry[pool_fields["replacementCategory"]],
            entry[pool_fields["replacementValue"]],
        ),
    )
    for role in ("preserve", "forbidValues"):
        field = fields[role]
        if field in normalized:
            normalized[field] = sorted(normalized[field])
    return normalized


def _batch_priority(rules: dict[str, Any]) -> list[str]:
    contract = rules["batchProductionContract"]
    return [
        rules["strategySources"][role]
        for role in contract["prioritySourceRoles"]
    ]


def _source_analysis_identity_valid(
    source_analysis: Any, rules: dict[str, Any]
) -> bool:
    target = source_analysis.get("target") if isinstance(source_analysis, dict) else None
    return bool(
        isinstance(target, dict)
        and target.get("category") in set(rules["sourceCategories"].values())
        and isinstance(target.get("role"), str)
        and target["role"].strip()
        and isinstance(target.get("identity"), str)
        and target["identity"].strip()
    )


def _merge_shared_policy_strategy(
    policy: dict[str, Any],
    per_image_strategy: dict[str, Any],
    assignment: dict[str, str],
    rules: dict[str, Any],
    batch_preserve_conflicts: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, dict[str, str]]]:
    contract = rules["batchProductionContract"]
    policy_fields = contract["sharedPolicyFields"]
    strategy_fields = rules["replacementStrategyContract"]["fieldRoles"]
    sources = rules["strategySources"]
    batch_source = sources["batchDecision"]
    per_image_source = sources["perImageDecision"]
    field_sources: dict[str, str] = {}
    effective: dict[str, Any] = {}
    for role in ("policyIdentity", "policyVersion"):
        strategy_field = strategy_fields[role]
        policy_field = policy_fields[role]
        if strategy_field in per_image_strategy:
            effective[strategy_field] = per_image_strategy[strategy_field]
            field_sources[strategy_field] = per_image_source
        else:
            effective[strategy_field] = policy[policy_field]
            field_sources[strategy_field] = batch_source
    for role in ("replacementValue", "replacementCategory"):
        strategy_field = strategy_fields[role]
        effective[strategy_field] = assignment[strategy_field]
        field_sources[strategy_field] = (
            per_image_source
            if strategy_field in per_image_strategy
            else batch_source
        )
    list_value_sources: dict[str, dict[str, str]] = {}
    batch_preserve_conflicts = batch_preserve_conflicts or set()
    for role in ("preserve", "forbidValues"):
        strategy_field = strategy_fields[role]
        policy_field = policy_fields[role]
        per_image_replacement = per_image_strategy.get(
            strategy_fields["replacementValue"]
        )
        value_sources = {
            value: batch_source
            for value in policy.get(policy_field, [])
            if value != per_image_replacement
            and not (
                role == "preserve" and value in batch_preserve_conflicts
            )
        }
        value_sources.update(
            {
                value: per_image_source
                for value in per_image_strategy.get(strategy_field, [])
            }
        )
        if value_sources:
            effective[strategy_field] = sorted(value_sources)
            field_sources[strategy_field] = (
                per_image_source
                if any(
                    source == per_image_source
                    for source in value_sources.values()
                )
                else batch_source
            )
            list_value_sources[strategy_field] = value_sources
    return effective, field_sources, list_value_sources


def _allocation_analysis_strategy(
    policy: dict[str, Any],
    per_image_strategy: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["batchProductionContract"]
    policy_fields = contract["sharedPolicyFields"]
    pool_fields = contract["replacementPoolEntryFields"]
    strategy_fields = rules["replacementStrategyContract"]["fieldRoles"]
    pool = policy[policy_fields["replacementPool"]]
    explicit_value = per_image_strategy.get(
        strategy_fields["replacementValue"]
    )
    explicit_category = per_image_strategy.get(
        strategy_fields["replacementCategory"]
    )
    first_entry = pool[0]
    assignment = {
        strategy_fields["replacementValue"]: (
            explicit_value
            if isinstance(explicit_value, str)
            else first_entry[pool_fields["replacementValue"]]
        ),
        strategy_fields["replacementCategory"]: (
            explicit_category
            if isinstance(explicit_category, str)
            else first_entry[pool_fields["replacementCategory"]]
        ),
    }
    effective, _, _ = _merge_shared_policy_strategy(
        policy,
        per_image_strategy,
        assignment,
        rules,
    )
    if not isinstance(explicit_value, str):
        effective.pop(strategy_fields["replacementValue"], None)
        effective.pop(strategy_fields["replacementCategory"], None)
    effective[contract["allocationAnalysisPoolField"]] = copy.deepcopy(pool)
    return effective


def _allocation_candidate_evaluations(
    source_analysis: dict[str, Any],
    policy: dict[str, Any],
    rules: dict[str, Any],
) -> list[dict[str, Any]] | None:
    contract = rules["batchProductionContract"]
    policy_fields = contract["sharedPolicyFields"]
    pool_fields = contract["replacementPoolEntryFields"]
    pool = policy[policy_fields["replacementPool"]]
    source_category = source_analysis["target"]["category"]
    raw_evaluations = source_analysis.get("replacementPool")
    if not isinstance(raw_evaluations, list):
        return None
    evaluation_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for evaluation in raw_evaluations:
        if not isinstance(evaluation, dict):
            return None
        category = evaluation.get("category")
        value = evaluation.get("value")
        if not isinstance(category, str) or not isinstance(value, str):
            return None
        key = (category, value)
        if key in evaluation_by_key:
            return None
        evaluation_by_key[key] = evaluation
    ordered: list[dict[str, Any]] = []
    for entry in pool:
        if entry[pool_fields["replacementCategory"]] != source_category:
            continue
        key = (
            entry[pool_fields["replacementCategory"]],
            entry[pool_fields["replacementValue"]],
        )
        evaluation = evaluation_by_key.get(key)
        if evaluation is None:
            return None
        ordered.append(copy.deepcopy(evaluation))
    return ordered


def _allocation_preserve_evaluations(
    source_analysis: dict[str, Any],
    policy: dict[str, Any],
    per_image_strategy: dict[str, Any],
    rules: dict[str, Any],
) -> list[dict[str, Any]] | None:
    contract = rules["batchProductionContract"]
    policy_fields = contract["sharedPolicyFields"]
    strategy_fields = rules["replacementStrategyContract"]["fieldRoles"]
    batch_values = {
        value
        for value in policy.get(policy_fields["preserve"], [])
        if value
        != per_image_strategy.get(strategy_fields["replacementValue"])
    }
    raw_evaluations = source_analysis.get("preserveConflictEvaluations", [])
    if not isinstance(raw_evaluations, list):
        return None
    dependency_type_field = rules["identityReplacementContract"][
        "dependencyFields"
    ]["dependencyType"]
    closure = source_analysis.get("dependencyClosure")
    if not isinstance(closure, list):
        return None
    changed_component_ids = {
        "primary-role",
        "primary-identity",
        *{
            f"dependency-{index}-{item.get(dependency_type_field)}"
            for index, item in enumerate(closure)
            if isinstance(item, dict)
            and isinstance(item.get(dependency_type_field), str)
        },
    }
    selected: dict[str, dict[str, Any]] = {}
    for evaluation in raw_evaluations:
        if not isinstance(evaluation, dict):
            return None
        value = evaluation.get("preserveValue")
        conflict = evaluation.get("conflictsWithChangedSet")
        component_ids = evaluation.get("changedComponentIds")
        if not isinstance(value, str) or value not in batch_values:
            continue
        if (
            not isinstance(conflict, bool)
            or not isinstance(component_ids, list)
            or not all(
                isinstance(component_id, str) and component_id
                for component_id in component_ids
            )
            or len(component_ids) != len(set(component_ids))
            or not set(component_ids) <= changed_component_ids
            or conflict is not bool(component_ids)
            or value in selected
        ):
            return None
        selected[value] = copy.deepcopy(evaluation)
    if set(selected) != batch_values:
        return None
    return [selected[value] for value in sorted(selected)]


def _batch_preserve_conflicts(
    evaluations: list[dict[str, Any]],
    per_image_strategy: dict[str, Any],
    rules: dict[str, Any],
) -> set[str]:
    replacement_field = rules["replacementStrategyContract"]["fieldRoles"][
        "replacementValue"
    ]
    if not isinstance(per_image_strategy.get(replacement_field), str):
        return set()
    return {
        evaluation["preserveValue"]
        for evaluation in evaluations
        if evaluation["conflictsWithChangedSet"] is True
    }


def _policy_resolution_valid(
    resolution: Any,
    *,
    batch_id: str,
    item_id: str,
    template_key: str,
    source_sha256: str,
    policy: dict[str, Any],
    per_image_strategy: dict[str, Any],
    rules: dict[str, Any],
) -> bool:
    contract = rules["batchProductionContract"]
    fields = contract["resolutionFields"]
    policy_fields = contract["sharedPolicyFields"]
    strategy_fields = rules["replacementStrategyContract"]["fieldRoles"]
    effective = resolution.get(fields["effectiveStrategy"]) if isinstance(resolution, dict) else None
    field_sources = resolution.get(fields["fieldSources"]) if isinstance(resolution, dict) else None
    value_sources = resolution.get(fields["listValueSources"]) if isinstance(resolution, dict) else None
    assignment = {
        strategy_fields[role]: effective.get(strategy_fields[role])
        for role in ("replacementValue", "replacementCategory")
    } if isinstance(effective, dict) else {}
    preserve_evaluations = (
        resolution.get(fields["allocationPreserveConflictEvaluations"])
        if isinstance(resolution, dict)
        else None
    )
    preserve_evaluations_valid = bool(
        isinstance(preserve_evaluations, list)
        and all(
            isinstance(evaluation, dict)
            and isinstance(evaluation.get("preserveValue"), str)
            and isinstance(evaluation.get("conflictsWithChangedSet"), bool)
            and isinstance(evaluation.get("changedComponentIds"), list)
            and all(
                isinstance(component_id, str) and component_id
                for component_id in evaluation["changedComponentIds"]
            )
            for evaluation in preserve_evaluations
        )
    )
    expected_effective, expected_field_sources, expected_value_sources = (
        _merge_shared_policy_strategy(
            policy,
            per_image_strategy,
            assignment,
            rules,
            _batch_preserve_conflicts(
                preserve_evaluations,
                per_image_strategy,
                rules,
            ),
        )
        if preserve_evaluations_valid
        and all(isinstance(value, str) and value for value in assignment.values())
        else ({}, {}, {})
    )
    pool_fields = contract["replacementPoolEntryFields"]
    assignment_key = (
        assignment.get(strategy_fields["replacementCategory"]),
        assignment.get(strategy_fields["replacementValue"]),
    )
    batch_assignment_valid = bool(
        expected_field_sources.get(strategy_fields["replacementValue"])
        != rules["strategySources"]["batchDecision"]
        or (
            assignment_key
            in {
                (
                    entry[pool_fields["replacementCategory"]],
                    entry[pool_fields["replacementValue"]],
                )
                for entry in policy[policy_fields["replacementPool"]]
            }
            and assignment_key[1]
            not in set(policy.get(policy_fields["forbidValues"], []))
        )
    )
    source_identity = (
        resolution.get(fields["sourceIdentity"])
        if isinstance(resolution, dict)
        else None
    )
    source_category = (
        resolution.get(fields["sourceCategory"])
        if isinstance(resolution, dict)
        else None
    )
    allocation_evaluations = (
        resolution.get(fields["allocationCandidateEvaluations"])
        if isinstance(resolution, dict)
        else None
    )
    pool_fields = contract["replacementPoolEntryFields"]
    expected_pool_keys = {
        (
            entry[pool_fields["replacementCategory"]],
            entry[pool_fields["replacementValue"]],
        )
        for entry in policy[policy_fields["replacementPool"]]
        if entry[pool_fields["replacementCategory"]] == source_category
    }
    evaluation_keys = (
        [
            (evaluation.get("category"), evaluation.get("value"))
            for evaluation in allocation_evaluations
        ]
        if isinstance(allocation_evaluations, list)
        and all(isinstance(evaluation, dict) for evaluation in allocation_evaluations)
        else []
    )
    return bool(
        isinstance(resolution, dict)
        and set(resolution) == set(fields.values())
        and resolution.get(fields["artifactType"]) == contract["resolutionArtifactType"]
        and resolution.get(fields["schemaVersion"]) == rules["schemaVersion"]
        and resolution.get(fields["batchIdentity"]) == batch_id
        and resolution.get(fields["productionItemIdentity"]) == item_id
        and resolution.get(fields["policyIdentity"])
        == policy[policy_fields["policyIdentity"]]
        and resolution.get(fields["policyVersion"])
        == policy[policy_fields["policyVersion"]]
        and resolution.get(fields["policyRevision"])
        == policy[policy_fields["policyRevision"]]
        and resolution.get(fields["policySha256"])
        == _sha_bytes(_canonical_bytes(policy))
        and resolution.get(fields["sourceImageSha256"]) == source_sha256
        and isinstance(source_identity, str)
        and source_identity.strip()
        and source_category in set(rules["sourceCategories"].values())
        and resolution.get(fields["scope"]) == policy[policy_fields["scope"]]
        and resolution.get(fields["priority"]) == _batch_priority(rules)
        and effective == expected_effective
        and batch_assignment_valid
        and not _replacement_strategy_errors(
            {"replacementStrategy": effective}, rules
        )
        and field_sources == expected_field_sources
        and value_sources == expected_value_sources
        and preserve_evaluations_valid
        and len(evaluation_keys) == len(expected_pool_keys)
        and len(evaluation_keys) == len(set(evaluation_keys))
        and set(evaluation_keys) == expected_pool_keys
        and resolution.get(fields["allocationSeed"])
        == f"{batch_id}+{template_key}+{source_identity}"
    )


def _shared_policy_plan_valid(
    plan: Any,
    resolution: Any,
    rules: dict[str, Any],
) -> bool:
    if not isinstance(plan, dict) or not isinstance(resolution, dict):
        return False
    batch_contract = rules["batchProductionContract"]
    resolution_fields = batch_contract["resolutionFields"]
    strategy_fields = rules["replacementStrategyContract"]["fieldRoles"]
    effective = resolution.get(resolution_fields["effectiveStrategy"])
    field_sources = resolution.get(resolution_fields["fieldSources"])
    targets = plan.get("primaryTargets")
    if not (
        isinstance(effective, dict)
        and isinstance(field_sources, dict)
        and isinstance(targets, list)
        and len(targets) == 1
        and isinstance(targets[0], dict)
    ):
        return False
    replacement_source = field_sources.get(
        strategy_fields["replacementValue"]
    )
    expected_plan_strategy = {
        "source": replacement_source,
        "decisionSource": replacement_source,
        **{
            field: effective[field]
            for field in (
                strategy_fields["policyIdentity"],
                strategy_fields["policyVersion"],
                strategy_fields["preserve"],
                strategy_fields["forbidValues"],
            )
            if field in effective
        },
    }
    target = targets[0]
    return bool(
        plan.get("strategy") == expected_plan_strategy
        and target.get("replacementValue")
        == effective.get(strategy_fields["replacementValue"])
        and target.get("replacementCategory")
        == effective.get(strategy_fields["replacementCategory"])
        and target.get("decisionSource") == replacement_source
    )


def _resolve_shared_policy(
    batch_id: str,
    items: list[dict[str, Any]],
    policy: dict[str, Any],
    output_root: Path,
    adapters: WorkflowAdapters,
    rules: dict[str, Any],
    invalid_item_ids: set[str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, WorkflowStop | None],
]:
    batch_contract = rules["batchProductionContract"]
    policy_fields = batch_contract["sharedPolicyFields"]
    pool_fields = batch_contract["replacementPoolEntryFields"]
    resolution_fields = batch_contract["resolutionFields"]
    strategy_fields = rules["replacementStrategyContract"]["fieldRoles"]
    normalized_policy = _normalize_shared_policy(policy, rules)
    policy_sha = _sha_bytes(_canonical_bytes(normalized_policy))
    scope = set(normalized_policy[policy_fields["scope"]])
    item_by_id = {str(item["productionItemId"]): item for item in items}
    source_analyses: dict[str, dict[str, Any]] = {}
    final_source_analyses: dict[str, dict[str, Any]] = {}
    source_shas: dict[str, str] = {}
    existing_resolutions: dict[str, dict[str, Any]] = {}
    assignments: dict[str, dict[str, str]] = {}
    compatible_candidate_keys: dict[str, set[tuple[str, str]]] = {}
    allocation_evaluations: dict[str, list[dict[str, Any]]] = {}
    preserve_evaluations: dict[str, list[dict[str, Any]]] = {}
    usage: dict[tuple[str, str], int] = {}
    preparation_failures: dict[str, WorkflowStop | None] = {}

    for item_id in sorted(scope):
        if item_id in invalid_item_ids:
            preparation_failures[item_id] = None
            continue
        item = item_by_id[item_id]
        source_value = item.get("sourceImage")
        if not isinstance(source_value, (str, os.PathLike)):
            preparation_failures[item_id] = None
            continue
        source_path = Path(source_value).resolve()
        if not source_path.is_file():
            preparation_failures[item_id] = None
            continue
        source_sha = _sha_file(source_path)
        source_shas[item_id] = source_sha
        per_image_strategy = _normalize_replacement_strategy(item, rules) or {}
        resolution_name = batch_contract["resolutionArtifactName"]
        resolution_path = (
            output_root / item_id / resolution_name
        )
        resolution_is_tracked = False
        if resolution_path.is_file():
            manifest_path = output_root / item_id / "production-manifest.json"
            try:
                existing = _load_json(resolution_path)
                persisted_manifest = (
                    _load_json(manifest_path)
                    if manifest_path.is_file()
                    else None
                )
                if not isinstance(existing, dict) or (
                    persisted_manifest is not None
                    and not isinstance(persisted_manifest, dict)
                ):
                    raise TypeError("tracked shared-policy evidence must be objects")
                manifest_artifacts = (
                    persisted_manifest.get("artifacts", {})
                    if isinstance(persisted_manifest, dict)
                    else {}
                )
                if not isinstance(manifest_artifacts, dict):
                    raise TypeError("manifest artifacts must be an object")
                artifact = manifest_artifacts.get(resolution_name)
                observed_resolution_sha = _sha_file(resolution_path)
                expected_scope_sha = _sha_bytes(
                    _canonical_bytes(
                        {
                            "productionItemId": item_id,
                            "artifact": resolution_name,
                            "sha256": observed_resolution_sha,
                        }
                    )
                )
                resolution_is_tracked = bool(
                    isinstance(artifact, dict)
                    and isinstance(persisted_manifest, dict)
                    and persisted_manifest.get("productionItemId") == item_id
                    and persisted_manifest.get("sourceImageSha256") == source_sha
                    and artifact.get("path") == resolution_name
                    and artifact.get("sha256") == observed_resolution_sha
                    and artifact.get("bytes") == resolution_path.stat().st_size
                    and artifact.get(
                        batch_contract["artifactScopeDigestField"]
                    )
                    == expected_scope_sha
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                existing = None
                if manifest_path.is_file():
                    preparation_failures[item_id] = _stop(
                        rules,
                        "blocked",
                        "productionItemIntegrityFailure",
                        "已跟踪的共享分辨或 Manifest 形状无效。",
                        {"productionItemId": item_id},
                    )
                    continue
            if resolution_is_tracked and _policy_resolution_valid(
                existing,
                batch_id=batch_id,
                item_id=item_id,
                template_key=item["templateKey"],
                source_sha256=source_sha,
                policy=normalized_policy,
                per_image_strategy=per_image_strategy,
                rules=rules,
            ):
                source_analysis_path = output_root / item_id / "source-analysis.json"
                source_artifact = persisted_manifest.get("artifacts", {}).get(
                    "source-analysis.json"
                )
                try:
                    persisted_source_analysis = _load_json(source_analysis_path)
                    if not isinstance(persisted_source_analysis, dict):
                        raise TypeError("source analysis must be an object")
                    observed_source_analysis_sha = _sha_file(
                        source_analysis_path
                    )
                    expected_source_scope_sha = _sha_bytes(
                        _canonical_bytes(
                            {
                                "productionItemId": item_id,
                                "artifact": "source-analysis.json",
                                "sha256": observed_source_analysis_sha,
                            }
                        )
                    )
                    source_analysis_is_tracked = bool(
                        isinstance(source_artifact, dict)
                        and source_artifact.get("path")
                        == "source-analysis.json"
                        and source_artifact.get("sha256")
                        == observed_source_analysis_sha
                        and source_artifact.get("bytes")
                        == source_analysis_path.stat().st_size
                        and source_artifact.get(
                            batch_contract["artifactScopeDigestField"]
                        )
                        == expected_source_scope_sha
                        and persisted_source_analysis.get(
                            "sourceImageSha256"
                        )
                        == source_sha
                        and _source_analysis_identity_valid(
                            persisted_source_analysis,
                            rules,
                        )
                        and persisted_source_analysis["target"]["identity"]
                        == existing[resolution_fields["sourceIdentity"]]
                        and persisted_source_analysis["target"]["category"]
                        == existing[resolution_fields["sourceCategory"]]
                        and _allocation_preserve_evaluations(
                            {
                                **persisted_source_analysis,
                                "preserveConflictEvaluations": existing[
                                    resolution_fields[
                                        "allocationPreserveConflictEvaluations"
                                    ]
                                ],
                            },
                            normalized_policy,
                            per_image_strategy,
                            rules,
                        )
                        is not None
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    source_analysis_is_tracked = False
                    preparation_failures[item_id] = _stop(
                        rules,
                        "blocked",
                        "productionItemIntegrityFailure",
                        "已跟踪的共享分配来源事实形状无效。",
                        {"productionItemId": item_id},
                    )
                    continue
                if source_analysis_is_tracked:
                    plan_path = output_root / item_id / "replacement-plan.json"
                    if plan_path.exists():
                        try:
                            persisted_plan = _load_json(plan_path)
                        except (
                            OSError,
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                        ):
                            persisted_plan = None
                        if not _shared_policy_plan_valid(
                            persisted_plan,
                            existing,
                            rules,
                        ):
                            preparation_failures[item_id] = _stop(
                                rules,
                                "blocked",
                                "productionItemIntegrityFailure",
                                "已跟踪的共享分辨与当前 Replacement Plan 不一致。",
                                {"productionItemId": item_id},
                            )
                            continue
                    existing_resolutions[item_id] = existing
                    source_analyses[item_id] = persisted_source_analysis
                    allocation_evaluations[item_id] = copy.deepcopy(
                        existing[
                            resolution_fields["allocationCandidateEvaluations"]
                        ]
                    )
                    preserve_evaluations[item_id] = copy.deepcopy(
                        existing[
                            resolution_fields[
                                "allocationPreserveConflictEvaluations"
                            ]
                        ]
                    )
                    continue
        try:
            allocation_strategy = _allocation_analysis_strategy(
                normalized_policy,
                per_image_strategy,
                rules,
            )
            source_analysis = _adapter_snapshot_image_object_call(
                rules,
                "analyze_source",
                adapters.analyze_source,
                source_path,
                source_sha,
                allocation_strategy,
            )
        except WorkflowStop as stop:
            preparation_failures[item_id] = stop
            continue
        if (
            source_analysis.get("sourceImageSha256") != source_sha
            or not _source_analysis_identity_valid(source_analysis, rules)
        ):
            preparation_failures[item_id] = _stop(
                rules,
                "failed",
                "externalFailure",
                "共享策略分配所用的来源分析事实无效。",
                {"productionItemId": item_id},
            )
            continue
        source_analyses[item_id] = source_analysis
        candidate_evaluations = _allocation_candidate_evaluations(
            source_analysis,
            normalized_policy,
            rules,
        )
        if candidate_evaluations is None:
            preparation_failures[item_id] = _stop(
                rules,
                "failed",
                "externalFailure",
                "共享候选分析未完整覆盖当前有界候选池。",
                {"productionItemId": item_id},
            )
            continue
        allocation_evaluations[item_id] = candidate_evaluations
        batch_preserve_evaluations = _allocation_preserve_evaluations(
            source_analysis,
            normalized_policy,
            per_image_strategy,
            rules,
        )
        if batch_preserve_evaluations is None:
            preparation_failures[item_id] = _stop(
                rules,
                "failed",
                "externalFailure",
                "共享保留项的优先级冲突证据无效。",
                {"productionItemId": item_id},
            )
            continue
        preserve_evaluations[item_id] = batch_preserve_evaluations

    def validate_final_assignment(
        item_id: str,
        assignment: dict[str, str],
    ) -> bool:
        item = item_by_id[item_id]
        per_image_strategy = _normalize_replacement_strategy(item, rules) or {}
        effective, _, _ = _merge_shared_policy_strategy(
            normalized_policy,
            per_image_strategy,
            assignment,
            rules,
            _batch_preserve_conflicts(
                preserve_evaluations[item_id],
                per_image_strategy,
                rules,
            ),
        )
        source_analysis = source_analyses[item_id]
        assignment_key = (
            assignment[strategy_fields["replacementCategory"]],
            assignment[strategy_fields["replacementValue"]],
        )
        evaluation = next(
            (
                candidate
                for candidate in allocation_evaluations[item_id]
                if (candidate.get("category"), candidate.get("value"))
                == assignment_key
            ),
            source_analysis.get("explicitReplacementEvaluation"),
        )
        if not isinstance(evaluation, dict):
            preparation_failures[item_id] = _stop(
                rules,
                "failed",
                "externalFailure",
                "共享分配缺少最终候选的类型化评估。",
                {"productionItemId": item_id},
            )
            return False
        allocation_analysis = copy.deepcopy(source_analysis)
        allocation_analysis["explicitReplacementEvaluation"] = copy.deepcopy(
            evaluation
        )
        retained_preserve = set(
            effective.get(strategy_fields["preserve"], [])
        )
        allocation_analysis["preserveConflictEvaluations"] = [
            copy.deepcopy(preserve_evaluation)
            for preserve_evaluation in allocation_analysis.get(
                "preserveConflictEvaluations", []
            )
            if preserve_evaluation.get("preserveValue") in retained_preserve
        ]
        try:
            _plan_replacement(
                allocation_analysis,
                rules,
                item["templateKey"],
                effective,
            )
        except WorkflowStop as stop:
            preparation_failures[item_id] = stop
            return False
        seed = (
            f"{batch_id}+{item['templateKey']}+"
            f"{source_analysis['target']['identity']}"
        )
        existing_resolution = existing_resolutions.get(item_id)
        reuse_existing_analysis = bool(
            isinstance(existing_resolution, dict)
            and existing_resolution.get(
                resolution_fields["effectiveStrategy"]
            )
            == effective
            and existing_resolution.get(
                resolution_fields["allocationSeed"]
            )
            == seed
        )
        if reuse_existing_analysis:
            final_source_analysis = copy.deepcopy(source_analysis)
        else:
            try:
                final_source_analysis = _adapter_snapshot_image_object_call(
                    rules,
                    "analyze_source",
                    adapters.analyze_source,
                    Path(item["sourceImage"]).resolve(),
                    source_shas[item_id],
                    copy.deepcopy(effective),
                )
            except WorkflowStop as stop:
                preparation_failures[item_id] = stop
                return False
        if (
            final_source_analysis.get("sourceImageSha256")
            != source_shas[item_id]
            or not _source_analysis_identity_valid(final_source_analysis, rules)
            or final_source_analysis["target"].get("identity")
            != source_analysis["target"].get("identity")
            or final_source_analysis["target"].get("category")
            != source_analysis["target"].get("category")
        ):
            preparation_failures[item_id] = _stop(
                rules,
                "failed",
                "externalFailure",
                "共享策略分配前后的单图输入事实不一致。",
                {"productionItemId": item_id},
            )
            return False
        try:
            _plan_replacement(
                final_source_analysis,
                rules,
                item["templateKey"],
                effective,
            )
        except WorkflowStop as stop:
            preparation_failures[item_id] = stop
            return False
        final_source_analyses[item_id] = final_source_analysis
        return True

    for item_id in sorted(existing_resolutions):
        if item_id in preparation_failures:
            continue
        existing_effective = existing_resolutions[item_id][
            resolution_fields["effectiveStrategy"]
        ]
        assignment = {
            strategy_fields["replacementValue"]: existing_effective[
                strategy_fields["replacementValue"]
            ],
            strategy_fields["replacementCategory"]: existing_effective[
                strategy_fields["replacementCategory"]
            ],
        }
        if validate_final_assignment(item_id, assignment):
            assignments[item_id] = assignment
            key = (
                assignment[strategy_fields["replacementCategory"]],
                assignment[strategy_fields["replacementValue"]],
            )
            usage[key] = usage.get(key, 0) + 1

    for item_id in sorted(scope):
        if item_id not in source_analyses or item_id in preparation_failures:
            continue
        if item_id in assignments:
            continue
        per_image_strategy = _normalize_replacement_strategy(
            item_by_id[item_id], rules
        ) or {}
        explicit_value = per_image_strategy.get(
            strategy_fields["replacementValue"]
        )
        explicit_category = per_image_strategy.get(
            strategy_fields["replacementCategory"]
        )
        if not (
            isinstance(explicit_value, str)
            and isinstance(explicit_category, str)
        ):
            continue
        assignment = {
            strategy_fields["replacementValue"]: explicit_value,
            strategy_fields["replacementCategory"]: explicit_category,
        }
        if validate_final_assignment(item_id, assignment):
            assignments[item_id] = assignment
            key = (explicit_category, explicit_value)
            usage[key] = usage.get(key, 0) + 1

    unresolved = [
        item_id
        for item_id in scope
        if item_id not in assignments
        and item_id in source_analyses
        and item_id not in preparation_failures
    ]
    unresolved.sort(
        key=lambda item_id: (
            _sha_bytes(
                _canonical_bytes(
                    {
                        batch_contract["requestFields"]["batchIdentity"]: batch_id,
                        "templateKey": item_by_id[item_id]["templateKey"],
                        "sourceIdentity": source_analyses[item_id]["target"]["identity"],
                    }
                )
            ),
            item_id,
        )
    )
    pool = normalized_policy[policy_fields["replacementPool"]]
    for item_id in unresolved:
        item = item_by_id[item_id]
        source_analysis = source_analyses[item_id]
        category = source_analysis["target"]["category"]
        per_image_strategy = _normalize_replacement_strategy(item, rules) or {}
        forbidden = set(
            normalized_policy.get(policy_fields["forbidValues"], [])
        )
        forbidden.update(
            per_image_strategy.get(strategy_fields["forbidValues"], [])
        )
        evaluation_by_key = {
            (evaluation.get("category"), evaluation.get("value")): evaluation
            for evaluation in allocation_evaluations[item_id]
        }
        candidate_stops: list[WorkflowStop] = []
        for entry in pool:
            candidate_value = entry[pool_fields["replacementValue"]]
            candidate_category = entry[pool_fields["replacementCategory"]]
            if candidate_category != category or candidate_value in forbidden:
                continue
            assignment = {
                strategy_fields["replacementValue"]: candidate_value,
                strategy_fields["replacementCategory"]: candidate_category,
            }
            effective, _, _ = _merge_shared_policy_strategy(
                normalized_policy,
                per_image_strategy,
                assignment,
                rules,
                _batch_preserve_conflicts(
                    preserve_evaluations[item_id],
                    per_image_strategy,
                    rules,
                ),
            )
            evaluation = evaluation_by_key.get(
                (candidate_category, candidate_value)
            )
            if evaluation is None:
                continue
            candidate_analysis = copy.deepcopy(source_analysis)
            candidate_analysis["explicitReplacementEvaluation"] = (
                copy.deepcopy(evaluation)
            )
            try:
                _plan_replacement(
                    candidate_analysis,
                    rules,
                    item["templateKey"],
                    effective,
                )
            except WorkflowStop as stop:
                candidate_stops.append(stop)
                continue
            compatible_candidate_keys.setdefault(item_id, set()).add(
                (candidate_category, candidate_value)
            )
        if not compatible_candidate_keys.get(item_id) and candidate_stops:
            review_stop = next(
                (
                    stop
                    for stop in candidate_stops
                    if stop.outcome == "needs_input"
                ),
                None,
            )
            failed_stop = next(
                (
                    stop
                    for stop in candidate_stops
                    if stop.outcome == "failed"
                ),
                None,
            )
            if review_stop is not None or failed_stop is not None:
                preparation_failures[item_id] = review_stop or failed_stop

    for item_id in unresolved:
        if item_id in preparation_failures:
            continue
        source_analysis = source_analyses[item_id]
        category = source_analysis["target"]["category"]
        per_image_strategy = _normalize_replacement_strategy(item_by_id[item_id], rules)
        forbidden = set(normalized_policy.get(policy_fields["forbidValues"], []))
        if per_image_strategy:
            forbidden.update(per_image_strategy.get(strategy_fields["forbidValues"], []))
        candidates = [
            entry
            for entry in pool
            if entry[pool_fields["replacementCategory"]] == category
            and entry[pool_fields["replacementValue"]] not in forbidden
            and (
                entry[pool_fields["replacementCategory"]],
                entry[pool_fields["replacementValue"]],
            )
            in compatible_candidate_keys.get(item_id, set())
        ]
        if not candidates:
            continue
        seed = (
            f"{batch_id}+{item_by_id[item_id]['templateKey']}+"
            f"{source_analysis['target']['identity']}"
        )
        selected = min(
            candidates,
            key=lambda entry: (
                usage.get(
                    (
                        entry[pool_fields["replacementCategory"]],
                        entry[pool_fields["replacementValue"]],
                    ),
                    0,
                ),
                _sha_bytes(
                    _canonical_bytes(
                        {
                            "seed": seed,
                            "category": entry[pool_fields["replacementCategory"]],
                            "value": entry[pool_fields["replacementValue"]],
                        }
                    )
                ),
            ),
        )
        value = selected[pool_fields["replacementValue"]]
        selected_category = selected[pool_fields["replacementCategory"]]
        assignment = {
            strategy_fields["replacementValue"]: value,
            strategy_fields["replacementCategory"]: selected_category,
        }
        if validate_final_assignment(item_id, assignment):
            assignments[item_id] = assignment
            key = (selected_category, value)
            usage[key] = usage.get(key, 0) + 1

    effective_requests: dict[str, dict[str, Any]] = {}
    resolutions: dict[str, dict[str, Any]] = {}
    for item_id in sorted(scope):
        item = item_by_id[item_id]
        if item_id not in source_analyses or item_id not in assignments:
            continue
        per_image_strategy = _normalize_replacement_strategy(item, rules) or {}
        assignment = assignments[item_id]
        effective, field_sources, list_value_sources = (
            _merge_shared_policy_strategy(
                normalized_policy,
                per_image_strategy,
                assignment,
                rules,
                _batch_preserve_conflicts(
                    preserve_evaluations[item_id],
                    per_image_strategy,
                    rules,
                ),
            )
        )
        source_analysis = final_source_analyses[item_id]
        seed = (
            f"{batch_id}+{item['templateKey']}+"
            f"{source_analysis['target']['identity']}"
        )
        effective_requests[item_id] = {
            **copy.deepcopy(item),
            "replacementStrategy": effective,
        }
        resolution = {
            resolution_fields["artifactType"]: batch_contract["resolutionArtifactType"],
            resolution_fields["schemaVersion"]: rules["schemaVersion"],
            resolution_fields["batchIdentity"]: batch_id,
            resolution_fields["productionItemIdentity"]: item_id,
            resolution_fields["policyIdentity"]: normalized_policy[
                policy_fields["policyIdentity"]
            ],
            resolution_fields["policyVersion"]: normalized_policy[
                policy_fields["policyVersion"]
            ],
            resolution_fields["policyRevision"]: normalized_policy[
                policy_fields["policyRevision"]
            ],
            resolution_fields["policySha256"]: policy_sha,
            resolution_fields["sourceImageSha256"]: source_shas[item_id],
            resolution_fields["sourceIdentity"]: source_analysis["target"]["identity"],
            resolution_fields["sourceCategory"]: source_analysis["target"]["category"],
            resolution_fields["scope"]: normalized_policy[policy_fields["scope"]],
            resolution_fields["priority"]: _batch_priority(rules),
            resolution_fields["effectiveStrategy"]: effective,
            resolution_fields["fieldSources"]: field_sources,
            resolution_fields["listValueSources"]: list_value_sources,
            resolution_fields["allocationCandidateEvaluations"]: copy.deepcopy(
                allocation_evaluations[item_id]
            ),
            resolution_fields[
                "allocationPreserveConflictEvaluations"
            ]: copy.deepcopy(preserve_evaluations[item_id]),
            resolution_fields["allocationSeed"]: seed,
        }
        source_analyses[item_id] = source_analysis
        resolutions[item_id] = resolution
    return effective_requests, source_analyses, resolutions, preparation_failures
