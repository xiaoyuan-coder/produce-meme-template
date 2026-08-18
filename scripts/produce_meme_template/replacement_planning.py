from __future__ import annotations

import copy
from typing import Any

from .release_management import runtime_production_pin
from .workflow_core import REPO_ROOT, _normalized_identity, _stop


def _build_pin(rules: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    del rules, release
    return runtime_production_pin(REPO_ROOT)


def _component_graph_view(
    graph: Any, rules: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    contract = rules["multiInstanceContract"]
    graph_fields = contract["graphFields"]
    component_fields = contract["componentFields"]
    relation_fields = contract["relationFields"]
    if not (
        isinstance(graph, dict)
        and set(graph) == set(graph_fields.values())
        and isinstance(graph.get(graph_fields["components"]), list)
        and graph[graph_fields["components"]]
        and isinstance(graph.get(graph_fields["relations"]), list)
        and isinstance(graph.get(graph_fields["explanation"]), str)
        and graph[graph_fields["explanation"]].strip()
    ):
        return None
    components = graph[graph_fields["components"]]
    relations = graph[graph_fields["relations"]]
    component_roles = set(contract["componentRoles"].values())
    if not all(
        isinstance(component, dict)
        and set(component) == set(component_fields.values())
        and isinstance(component.get(component_fields["identity"]), str)
        and component[component_fields["identity"]].strip()
        and isinstance(component.get(component_fields["role"]), str)
        and component.get(component_fields["role"]) in component_roles
        and (
            component.get(component_fields["identityUnit"]) is None
            or isinstance(component.get(component_fields["identityUnit"]), str)
            and component[component_fields["identityUnit"]].strip()
        )
        and isinstance(component.get(component_fields["visualInstance"]), bool)
        and all(
            component.get(component_fields[field]) is None
            or isinstance(component.get(component_fields[field]), str)
            and component[component_fields[field]].strip()
            for field in ("uploadAsset", "control", "container")
        )
        and isinstance(component.get(component_fields["explanation"]), str)
        and component[component_fields["explanation"]].strip()
        for component in components
    ):
        return None
    component_ids = [component[component_fields["identity"]] for component in components]
    if len(component_ids) != len(set(component_ids)):
        return None
    component_id_set = set(component_ids)
    if any(
        component[component_fields["container"]] is not None
        and (
            component[component_fields["container"]] not in component_id_set
            or component[component_fields["container"]]
            == component[component_fields["identity"]]
        )
        for component in components
    ):
        return None
    container_by_component = {
        component[component_fields["identity"]]: component[component_fields["container"]]
        for component in components
    }
    for component_id in component_id_set:
        visited: set[str] = set()
        current: str | None = component_id
        while current is not None:
            if current in visited:
                return None
            visited.add(current)
            current = container_by_component[current]
    relation_types = set(contract["relationTypes"].values())
    if not all(
        isinstance(relation, dict)
        and set(relation) == set(relation_fields.values())
        and isinstance(relation.get(relation_fields["identity"]), str)
        and relation[relation_fields["identity"]].strip()
        and isinstance(relation.get(relation_fields["type"]), str)
        and relation.get(relation_fields["type"]) in relation_types
        and isinstance(relation.get(relation_fields["source"]), str)
        and relation.get(relation_fields["source"]) in component_id_set
        and isinstance(relation.get(relation_fields["target"]), str)
        and relation.get(relation_fields["target"]) in component_id_set
        and relation[relation_fields["source"]] != relation[relation_fields["target"]]
        and isinstance(relation.get(relation_fields["explanation"]), str)
        and relation[relation_fields["explanation"]].strip()
        for relation in relations
    ):
        return None
    relation_ids = [relation[relation_fields["identity"]] for relation in relations]
    if len(relation_ids) != len(set(relation_ids)):
        return None
    return components, relations


def _identity_relations_are_consistent(
    components: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    contract: dict[str, Any],
) -> bool:
    component_fields = contract["componentFields"]
    relation_fields = contract["relationFields"]
    component_by_id = {
        component[component_fields["identity"]]: component for component in components
    }
    identity_relation_types = {
        contract["relationTypes"][role]
        for role in contract["identityDerivedRelationTypeKeys"]
    }
    return all(
        relation[relation_fields["type"]] not in identity_relation_types
        or (
            component_by_id[relation[relation_fields["source"]]][
                component_fields["identityUnit"]
            ]
            is not None
            and component_by_id[relation[relation_fields["source"]]][
                component_fields["identityUnit"]
            ]
            == component_by_id[relation[relation_fields["target"]]][
                component_fields["identityUnit"]
            ]
        )
        for relation in relations
    )


def _complete_typed_relation_chain(
    node_ids: set[str],
    relations: list[dict[str, Any]],
    relation_type: str,
    relation_fields: dict[str, str],
) -> bool:
    if len(node_ids) < 2:
        return False
    chain_relations = [
        relation
        for relation in relations
        if relation[relation_fields["type"]] == relation_type
        and relation[relation_fields["source"]] in node_ids
        and relation[relation_fields["target"]] in node_ids
    ]
    if len(chain_relations) != len(node_ids) - 1:
        return False
    outgoing: dict[str, str] = {}
    incoming: dict[str, str] = {}
    for relation in chain_relations:
        source_id = relation[relation_fields["source"]]
        target_id = relation[relation_fields["target"]]
        if source_id in outgoing or target_id in incoming:
            return False
        outgoing[source_id] = target_id
        incoming[target_id] = source_id
    starts = node_ids - set(incoming)
    if len(starts) != 1:
        return False
    visited: set[str] = set()
    current = next(iter(starts))
    while current not in visited:
        visited.add(current)
        if current not in outgoing:
            break
        current = outgoing[current]
    return visited == node_ids


def _validated_source_multi_instance_contract(
    source_analysis: dict[str, Any], rules: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    contract = rules["multiInstanceContract"]
    source_fields = contract["sourceFields"]
    graph = source_analysis.get(source_fields["componentGraph"])
    graph_view = _component_graph_view(graph, rules)
    operations = source_analysis.get(source_fields["imageOperations"])
    if graph_view is None or not isinstance(operations, list) or not operations:
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源组件图与图片操作必须提供完整结构化证据。",
            {},
        )
    components, relations = graph_view
    component_fields = contract["componentFields"]
    relation_fields = contract["relationFields"]
    operation_fields = contract["operationFields"]
    component_by_id = {
        component[component_fields["identity"]]: component for component in components
    }
    relation_by_id = {
        relation[relation_fields["identity"]]: relation for relation in relations
    }
    operation_values = set(contract["operations"].values())
    list_field_roles = (
        "targetRegions",
        "clearRequirements",
        "stableAnchors",
        "preservedRelations",
    )
    operation_shape_valid = all(
        isinstance(operation, dict)
        and set(operation) == set(operation_fields.values())
        and isinstance(operation.get(operation_fields["identity"]), str)
        and operation[operation_fields["identity"]].strip()
        and isinstance(operation.get(operation_fields["operation"]), str)
        and operation.get(operation_fields["operation"]) in operation_values
        and all(
            isinstance(operation.get(operation_fields[field]), list)
            and all(
                isinstance(value, str) and value.strip()
                for value in operation[operation_fields[field]]
            )
            and len(operation[operation_fields[field]])
            == len(set(operation[operation_fields[field]]))
            for field in list_field_roles
        )
        and operation[operation_fields["targetRegions"]]
        and operation[operation_fields["clearRequirements"]]
        and operation[operation_fields["stableAnchors"]]
        and set(operation[operation_fields["targetRegions"]]) <= set(component_by_id)
        and set(operation[operation_fields["stableAnchors"]]) <= set(component_by_id)
        and not (
            set(operation[operation_fields["targetRegions"]])
            & set(operation[operation_fields["stableAnchors"]])
        )
        and set(operation[operation_fields["preservedRelations"]]) <= set(relation_by_id)
        and isinstance(operation.get(operation_fields["explanation"]), str)
        and operation[operation_fields["explanation"]].strip()
        for operation in operations
    )
    if not operation_shape_valid or len(
        {operation[operation_fields["identity"]] for operation in operations}
    ) != len(operations):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "图片操作必须唯一声明合法目标、清除要求、稳定锚点和保持关系。",
            {},
        )
    operation_target_lists = [
        operation[operation_fields["targetRegions"]] for operation in operations
    ]
    flattened_operation_targets = [
        target for targets in operation_target_lists for target in targets
    ]
    if len(flattened_operation_targets) != len(set(flattened_operation_targets)):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "一个组件只能由一个图片操作负责。",
            {},
        )
    preservation_types = {
        contract["relationTypes"][role]
        for role in contract["relationTypeKeysRequiringPreservation"]
    }
    relation_coverage_valid = all(
        {
            relation[relation_fields["identity"]]
            for relation in relations
            if relation[relation_fields["type"]] in preservation_types
            and (
                relation[relation_fields["source"]]
                in set(operation[operation_fields["targetRegions"]])
                or relation[relation_fields["target"]]
                in set(operation[operation_fields["targetRegions"]])
            )
        }
        <= set(operation[operation_fields["preservedRelations"]])
        for operation in operations
    )
    identity_relations_valid = _identity_relations_are_consistent(
        components, relations, contract
    )
    identity_coverage_valid = all(
        {
            component[component_fields["identity"]]
            for component in components
            if component[component_fields["identityUnit"]]
            in {
                component_by_id[target][component_fields["identityUnit"]]
                for target in operation[operation_fields["targetRegions"]]
                if component_by_id[target][component_fields["identityUnit"]] is not None
            }
        }
        <= set(operation[operation_fields["targetRegions"]])
        for operation in operations
    )
    requirement_by_operation = {
        contract["operations"][role]: requirement
        for role, requirement in contract["operationRequirements"].items()
    }

    def operation_semantics_valid(operation: dict[str, Any]) -> bool:
        requirement = requirement_by_operation[operation[operation_fields["operation"]]]
        target_ids = set(operation[operation_fields["targetRegions"]])
        anchor_ids = set(operation[operation_fields["stableAnchors"]])
        preserved_ids = set(operation[operation_fields["preservedRelations"]])
        target_components = [component_by_id[value] for value in target_ids]
        anchor_components = [component_by_id[value] for value in anchor_ids]
        required_target_roles = {
            contract["componentRoles"][role]
            for role in requirement["requiredTargetRoleKeys"]
        }
        allowed_target_roles = {
            contract["componentRoles"][role]
            for role in requirement["allowedTargetRoleKeys"]
        }
        required_anchor_roles = {
            contract["componentRoles"][role]
            for role in requirement["requiredAnchorRoleKeys"]
        }
        identity_units = {
            component[component_fields["identityUnit"]]
            for component in target_components
        }
        target_containers = {
            component[component_fields["container"]]
            for component in target_components
        }
        required_relation_types = {
            contract["relationTypes"][role]
            for role in requirement["requiredRelationTypeKeys"]
        }
        operation_scope = target_ids | anchor_ids
        scoped_required_relations = [
            relation
            for relation in relations
            if relation[relation_fields["type"]] in required_relation_types
            and {
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            }
            <= operation_scope
        ]
        ordered_chain_valid = True
        if requirement["requiresCompleteOrderedChain"]:
            ordered_chain_valid = _complete_typed_relation_chain(
                target_containers,
                relations,
                contract["relationTypes"]["orderedBefore"],
                relation_fields,
            )
        return bool(
            len(target_ids) >= requirement["minimumTargets"]
            and required_target_roles
            <= {component[component_fields["role"]] for component in target_components}
            and (
                not allowed_target_roles
                or {
                    component[component_fields["role"]]
                    for component in target_components
                }
                <= allowed_target_roles
            )
            and required_anchor_roles
            <= {component[component_fields["role"]] for component in anchor_components}
            and (
                not requirement["singleIdentityUnit"]
                or None not in identity_units
                and len(identity_units) == 1
            )
            and (
                not requirement["targetContainersMustBeAnchors"]
                or None not in target_containers
                and target_containers <= anchor_ids
            )
            and (
                not required_relation_types
                or required_relation_types
                <= {
                    relation[relation_fields["type"]]
                    for relation in scoped_required_relations
                }
                and {
                    relation[relation_fields["identity"]]
                    for relation in scoped_required_relations
                }
                <= preserved_ids
            )
            and all(
                {
                    relation_by_id[relation_id][relation_fields["source"]],
                    relation_by_id[relation_id][relation_fields["target"]],
                }
                <= operation_scope
                for relation_id in preserved_ids
            )
            and ordered_chain_valid
        )

    operation_semantics_are_valid = all(
        operation_semantics_valid(operation) for operation in operations
    )
    if not (
        relation_coverage_valid
        and identity_relations_valid
        and identity_coverage_valid
        and operation_semantics_are_valid
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "图片操作类型、身份闭包、派生关系或接触遮挡保持证据无效。",
            {},
        )
    dependency_fields = rules["identityReplacementContract"]["dependencyFields"]
    closure_component_field = dependency_fields["componentIdentity"]
    closure = source_analysis.get("dependencyClosure", [])
    named_closure_ids = [
        item.get(closure_component_field) for item in closure if isinstance(item, dict)
    ]
    if not (
        len(named_closure_ids) == len(closure)
        and all(isinstance(value, str) and value.strip() for value in named_closure_ids)
        and len(named_closure_ids) == len(set(named_closure_ids))
    ):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "具名依赖闭包必须为每个组件提供唯一非空 ID。",
            {},
        )
    operation_target_ids = {
        target
        for operation in operations
        for target in operation[operation_fields["targetRegions"]]
    }
    if operation_target_ids != set(named_closure_ids):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "图片操作目标必须精确覆盖具名依赖闭包。",
            {
                "missingOperationTargets": sorted(set(named_closure_ids) - operation_target_ids),
                "targetsOutsideClosure": sorted(operation_target_ids - set(named_closure_ids)),
            },
        )
    identity_contract = rules["identityReplacementContract"]
    closure_type_field = identity_contract["dependencyFields"]["dependencyType"]
    closure_by_id = {item[closure_component_field]: item for item in closure}
    identity_dependency_role_by_value = {
        value: role for role, value in identity_contract["dependencyTypes"].items()
    }
    operation_role_by_value = {
        value: role for role, value in contract["operations"].items()
    }

    def identity_dependency_matches_component(component_id: str) -> bool:
        item = closure_by_id[component_id]
        dependency_type = item[closure_type_field]
        dependency_role = identity_dependency_role_by_value.get(dependency_type)
        if (
            dependency_role is None
            or dependency_role
            not in identity_contract["dependencyComponentRoleKeys"]
            or dependency_role
            not in identity_contract["dependencyRelationTypeKeys"]
        ):
            return False
        component = component_by_id[component_id]
        allowed_roles = {
            contract["componentRoles"][role]
            for role in identity_contract["dependencyComponentRoleKeys"][dependency_role]
        }
        required_relation_types = {
            contract["relationTypes"][role]
            for role in identity_contract["dependencyRelationTypeKeys"][dependency_role]
        }
        observed_relation_types = {
            relation[relation_fields["type"]]
            for relation in relations
            if component_id
            in {
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            }
        }
        if component[component_fields["role"]] not in allowed_roles:
            return False
        return not required_relation_types or bool(
            required_relation_types & observed_relation_types
        )

    dependency_topology_valid = all(
        (
            all(
                identity_dependency_matches_component(component_id)
                for component_id in operation[operation_fields["targetRegions"]]
            )
            if operation_role_by_value[operation[operation_fields["operation"]]]
            == "identityReplace"
            else all(
                closure_by_id[component_id][closure_type_field]
                == identity_contract["dependencyTypes"][
                    contract["operationDependencyTypes"][
                        operation_role_by_value[operation[operation_fields["operation"]]]
                    ]
                ]
                for component_id in operation[operation_fields["targetRegions"]]
            )
        )
        for operation in operations
    )
    if not dependency_topology_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "依赖类型必须与图片操作、组件角色和关系拓扑一致。",
            {},
        )
    return copy.deepcopy(graph), copy.deepcopy(operations)


def _plan_replacement(
    source_analysis: dict[str, Any],
    rules: dict[str, Any],
    template_key: str,
    replacement_strategy: dict[str, Any] | None = None,
    shared_policy_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category = source_analysis["target"]["category"]
    categories = rules["sourceCategories"]
    category_values = set(categories.values())
    if category == categories["unknownCategory"] or category not in category_values:
        raise _stop(
            rules,
            "needs_input",
            "unknownCategory",
            "来源主体类别无法支持自主替换，需要补充识别。",
            {"category": category},
        )
    eligibility = source_analysis.get("targetEligibility", {})
    if not isinstance(eligibility, dict):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源分析的 targetEligibility 必须是对象。",
            {"actualType": type(eligibility).__name__},
        )
    identity_contract = rules["identityReplacementContract"]
    identity_modifiers = list(identity_contract["identityEquivalenceModifiers"].values())
    dependency_fields = identity_contract["dependencyFields"]
    component_field = dependency_fields["componentIdentity"]
    type_field = dependency_fields["dependencyType"]
    value_field = dependency_fields["description"]
    closure = source_analysis.get("dependencyClosure", [])
    if not isinstance(closure, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get(component_field), str)
        and item[component_field].strip()
        and isinstance(item.get(type_field), str)
        and item[type_field].strip()
        and isinstance(item.get(value_field), str)
        and item[value_field].strip()
        for item in closure
    ):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源分析的 dependencyClosure 必须是包含非空 type/value 的对象列表。",
            {"actualType": type(closure).__name__},
        )
    if len({item[component_field] for item in closure}) != len(closure):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源分析的 dependencyClosure 组件 ID 必须唯一。",
            {},
        )
    if not closure:
        raise _stop(
            rules,
            "needs_input",
            "riskNeedsReview",
            "主要替换目标的依赖范围尚无法可靠判定，需要复核。",
            {"category": category},
        )
    component_graph, image_operations = _validated_source_multi_instance_contract(
        source_analysis, rules
    )
    identity_route_role = next(
        (
            role
            for role, route in identity_contract["routes"].items()
            if category == categories[route["sourceCategoryRole"]]
        ),
        None,
    )
    identity_route = (
        identity_contract["routes"][identity_route_role]
        if identity_route_role is not None
        else None
    )
    identity_context: dict[str, Any] | None = None
    if identity_route is not None:
        identity_target_valid = bool(
            isinstance(source_analysis.get("target"), dict)
            and isinstance(source_analysis["target"].get("role"), str)
            and source_analysis["target"]["role"].strip()
            and isinstance(source_analysis["target"].get("identity"), str)
            and source_analysis["target"]["identity"].strip()
            and _normalized_identity(
                source_analysis["target"]["identity"], identity_modifiers
            )
        )
        if not identity_target_valid:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "身份路由的来源角色与来源身份必须是非空字符串。",
                {"identityRouteRole": identity_route_role},
            )
        source_fields = identity_contract["sourceFields"]
        route_evidence_fields = identity_contract["routeEvidenceFields"]
        route_evidence = source_analysis.get(source_fields["routeEvidence"])
        route_evidence_valid = bool(
            isinstance(route_evidence, dict)
            and set(route_evidence) == set(route_evidence_fields.values())
            and route_evidence.get(route_evidence_fields["mode"]) == identity_route["mode"]
            and route_evidence.get(route_evidence_fields["localAssetRequirement"])
            is identity_route["localAssetRequired"]
            and route_evidence.get(route_evidence_fields["completeRedraw"]) is True
            and isinstance(route_evidence.get(route_evidence_fields["explanation"]), str)
            and route_evidence[route_evidence_fields["explanation"]].strip()
        )
        dependency_types = set(identity_contract["dependencyTypes"].values())
        closure_components_valid = bool(
            all(
                isinstance(item.get(component_field), str)
                and item[component_field].strip()
                and item.get(type_field) in dependency_types
                and isinstance(item.get(value_field), str)
                and item[value_field].strip()
                for item in closure
            )
            and len({item[component_field] for item in closure}) == len(closure)
        )
        topology_fields = identity_contract["topologyFields"]
        topology = source_analysis.get(source_fields["topology"])
        closure_component_ids = (
            {item[component_field] for item in closure}
            if closure_components_valid
            else set()
        )
        closure_identity_text_ids = (
            {
                item[component_field]
                for item in closure
                if item[type_field] == identity_contract["dependencyTypes"]["identityText"]
            }
            if closure_components_valid
            else set()
        )
        closure_full_body_ids = (
            {
                item[component_field]
                for item in closure
                if item[type_field] == identity_contract["dependencyTypes"]["fullBody"]
            }
            if closure_components_valid
            else set()
        )
        topology_valid = bool(
            isinstance(topology, dict)
            and set(topology) == set(topology_fields.values())
            and isinstance(topology.get(topology_fields["requiredComponents"]), list)
            and topology[topology_fields["requiredComponents"]]
            and all(
                isinstance(value, str) and value.strip()
                for value in topology[topology_fields["requiredComponents"]]
            )
            and len(topology[topology_fields["requiredComponents"]])
            == len(set(topology[topology_fields["requiredComponents"]]))
            and set(topology[topology_fields["requiredComponents"]]) == closure_component_ids
            and closure_full_body_ids
            and closure_full_body_ids
            <= set(topology[topology_fields["requiredComponents"]])
            and isinstance(topology.get(topology_fields["identityTextComponents"]), list)
            and all(
                isinstance(value, str) and value.strip()
                for value in topology[topology_fields["identityTextComponents"]]
            )
            and len(topology[topology_fields["identityTextComponents"]])
            == len(set(topology[topology_fields["identityTextComponents"]]))
            and set(topology[topology_fields["identityTextComponents"]])
            == closure_identity_text_ids
            and closure_identity_text_ids
            <= set(topology[topology_fields["requiredComponents"]])
            and isinstance(topology.get(topology_fields["explanation"]), str)
            and topology[topology_fields["explanation"]].strip()
        )
        if not (route_evidence_valid and closure_components_valid and topology_valid):
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "身份路由、依赖组件或身份拓扑证据无效。",
                {"identityRouteRole": identity_route_role},
            )
        identity_context = {
            "routeEvidence": copy.deepcopy(route_evidence),
            "topology": copy.deepcopy(topology),
            "textComponentIds": set(topology[topology_fields["identityTextComponents"]]),
            "closureByComponent": {item[component_field]: item for item in closure},
        }
    explicit_text_authorization = bool(
        replacement_strategy
        and replacement_strategy.get("replacementValue")
        and replacement_strategy.get("replacementCategory") == categories["textContent"]
    )
    if category == categories["textContent"] and not (
        explicit_text_authorization or eligibility.get("textRewriteRequiredByMechanism") is True
    ):
        raise _stop(
            rules,
            "blocked",
            "noCompatibleReplacement",
            "原图文字未获显式替换授权，且画面机制不要求等价重写。",
            {"category": category},
        )
    explicit_scene_authorization = bool(
        replacement_strategy
        and replacement_strategy.get("replacementValue")
        and replacement_strategy.get("replacementCategory") == categories["sceneAttribute"]
    )
    autonomous_scene_eligible = bool(
        eligibility.get("primarySubjectHasReplacementValue") is False
        and eligibility.get("sceneChangeCreatesStableTemplateValue") is True
    )
    if category == categories["sceneAttribute"] and not (
        explicit_scene_authorization or autonomous_scene_eligible
    ):
        raise _stop(
            rules,
            "blocked",
            "noCompatibleReplacement",
            "场景替换仅在主体缺少替换价值且场景变化能形成稳定模板价值时启用。",
            {"category": category, "targetEligibility": eligibility},
        )

    distinct_identity_field = identity_contract["candidateFields"]["distinctIdentityEvidence"]
    distinct_identity_fields = identity_contract["distinctIdentityEvidenceFields"]

    def distinct_identity_evidence_shape_valid(candidate: Any) -> bool:
        evidence = (
            candidate.get(distinct_identity_field) if isinstance(candidate, dict) else None
        )
        return bool(
            isinstance(evidence, dict)
            and set(evidence) == set(distinct_identity_fields.values())
            and evidence.get(distinct_identity_fields["sourceIdentity"])
            == source_analysis["target"]["identity"]
            and evidence.get(distinct_identity_fields["candidateIdentity"])
            == candidate.get("value")
            and isinstance(evidence.get(distinct_identity_fields["distinct"]), bool)
            and isinstance(evidence.get(distinct_identity_fields["explanation"]), str)
            and evidence[distinct_identity_fields["explanation"]].strip()
        )

    def identity_candidate_is_semantically_new(candidate: dict[str, Any]) -> bool:
        normalized_source = _normalized_identity(
            source_analysis["target"]["identity"], identity_modifiers
        )
        normalized_candidate = _normalized_identity(candidate["value"], identity_modifiers)
        return bool(
            normalized_source
            and normalized_candidate
            and normalized_candidate != normalized_source
        )

    def compatible_candidate(candidate: Any) -> bool:
        return bool(
            isinstance(candidate, dict)
            and isinstance(candidate.get("value"), str)
            and candidate.get("value").strip()
            and candidate.get("category") == category
            and candidate.get("semanticCompatible") is True
            and candidate.get("visualCompatible") is True
            and isinstance(candidate.get("rightsAndSafety"), str)
            and isinstance(candidate.get("reason"), str)
            and candidate.get("reason").strip()
            and isinstance(candidate.get("score"), (int, float))
            and not isinstance(candidate.get("score"), bool)
            and (
                identity_route is None
                or (
                    distinct_identity_evidence_shape_valid(candidate)
                    and candidate[distinct_identity_field][
                        distinct_identity_fields["distinct"]
                    ]
                    is True
                    and identity_candidate_is_semantically_new(candidate)
                )
            )
        )

    def hard_valid(candidate: Any) -> bool:
        return compatible_candidate(candidate) and candidate.get("rightsAndSafety") == "pass"

    replacement_pool = source_analysis.get("replacementPool", [])
    if not isinstance(replacement_pool, list):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源分析的 replacementPool 必须是列表。",
            {"actualType": type(replacement_pool).__name__},
        )
    if identity_route is not None:
        malformed_distinct_identity_candidates = [
            candidate
            for candidate in replacement_pool
            if isinstance(candidate, dict)
            and candidate.get("category") == category
            and not distinct_identity_evidence_shape_valid(candidate)
        ]
        if malformed_distinct_identity_candidates:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "身份候选缺少与来源值、候选值双向绑定的 distinctIdentity 证据。",
                {"identityRouteRole": identity_route_role},
            )
    if identity_route is not None and identity_route["candidateCardRequired"]:
        candidate_card_field = identity_contract["candidateFields"]["card"]
        candidate_card_fields = identity_contract["candidateCardFields"]

        def candidate_card_valid(candidate: Any) -> bool:
            card = candidate.get(candidate_card_field) if isinstance(candidate, dict) else None
            return bool(
                isinstance(card, dict)
                and set(card) == set(candidate_card_fields.values())
                and all(
                    isinstance(card.get(field), list)
                    and card[field]
                    and all(isinstance(value, str) and value.strip() for value in card[field])
                    and len(card[field]) == len(set(card[field]))
                    for field in candidate_card_fields.values()
                )
            )

        malformed_identity_candidates = [
            candidate
            for candidate in replacement_pool
            if isinstance(candidate, dict)
            and candidate.get("category") == category
            and not candidate_card_valid(candidate)
        ]
        if malformed_identity_candidates:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "公众人物或知名 IP 候选卡缺少身份锚点、反锚点或玩法融合要求。",
                {"identityRouteRole": identity_route_role},
            )
    candidates = [
        candidate
        for candidate in replacement_pool
        if hard_valid(candidate)
    ]
    review_candidates = [
        candidate
        for candidate in replacement_pool
        if compatible_candidate(candidate) and candidate.get("rightsAndSafety") == "review"
    ]
    strategy_sources = rules["strategySources"]
    autonomous_source = strategy_sources["autonomousDecision"]
    per_image_source = strategy_sources["perImageDecision"]
    strategy_field_roles = rules["replacementStrategyContract"]["fieldRoles"]
    resolution_field_sources: dict[str, str] = {}
    resolution_value_sources: dict[str, dict[str, str]] = {}
    if shared_policy_resolution is not None:
        resolution_fields = rules["batchProductionContract"]["resolutionFields"]
        raw_field_sources = shared_policy_resolution.get(
            resolution_fields["fieldSources"]
        )
        raw_value_sources = shared_policy_resolution.get(
            resolution_fields["listValueSources"]
        )
        if isinstance(raw_field_sources, dict):
            resolution_field_sources = raw_field_sources
        if isinstance(raw_value_sources, dict):
            resolution_value_sources = raw_value_sources
    decision_source = autonomous_source
    strategy = {"source": autonomous_source, "decisionSource": autonomous_source}
    preserve_values: list[str] = []
    if replacement_strategy:
        forbidden_values = {
            value for value in replacement_strategy.get("forbidValues", []) if isinstance(value, str) and value
        }
        preserve_values = sorted(
            value for value in replacement_strategy.get("preserve", []) if isinstance(value, str) and value
        )
        candidates = [candidate for candidate in candidates if candidate["value"] not in forbidden_values]
        review_candidates = [
            candidate for candidate in review_candidates if candidate["value"] not in forbidden_values
        ]
        replacement_decision_source = resolution_field_sources.get(
            strategy_field_roles["replacementValue"], per_image_source
        )
        strategy = {
            "source": replacement_decision_source,
            "decisionSource": replacement_decision_source,
            **{
                key: replacement_strategy[key]
                for key in ("policyId", "policyVersion")
                if replacement_strategy.get(key) is not None
            },
            **({"forbidValues": sorted(forbidden_values)} if forbidden_values else {}),
            **({"preserve": preserve_values} if preserve_values else {}),
        }
        if not candidates and replacement_strategy.get("replacementValue") is None:
            if review_candidates:
                raise _stop(
                    rules,
                    "needs_input",
                    "riskNeedsReview",
                    "单图策略过滤后只剩权利或安全风险待判断的候选，需要复核。",
                    {"category": category, "candidateValues": [item["value"] for item in review_candidates]},
                )
            raise _stop(
                rules,
                "blocked",
                "noCompatibleReplacement",
                "单图策略过滤后没有兼容的替换值。",
                {"category": category, "forbidValues": sorted(forbidden_values)},
            )
    if replacement_strategy and replacement_strategy.get("replacementValue") is not None:
        requested_value = replacement_strategy["replacementValue"]
        requested_category = replacement_strategy["replacementCategory"]
        selected = source_analysis.get("explicitReplacementEvaluation")
        if identity_route is not None and not distinct_identity_evidence_shape_valid(selected):
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "显式身份替换值缺少双向绑定的 distinctIdentity 证据。",
                {"identityRouteRole": identity_route_role},
            )
        exact_evaluation = bool(
            compatible_candidate(selected)
            and selected.get("value") == requested_value
            and selected.get("category") == requested_category
            and requested_value not in forbidden_values
        )
        if exact_evaluation and selected.get("rightsAndSafety") == "review":
            raise _stop(
                rules,
                "needs_input",
                "riskNeedsReview",
                "单图显式替换值的权利或安全风险仍待判断，需要复核。",
                {"replacementValue": requested_value, "replacementCategory": requested_category},
            )
        if not (
            exact_evaluation and selected.get("rightsAndSafety") == "pass"
        ):
            raise _stop(
                rules,
                "blocked",
                "explicitStrategyConflict",
                "单图显式替换值没有通过类别、视觉或权利硬过滤。",
                {
                    "replacementValue": requested_value,
                    "replacementCategory": requested_category,
                },
            )
        decision_source = resolution_field_sources.get(
            strategy_field_roles["replacementValue"], per_image_source
        )
    else:
        if not candidates:
            if review_candidates:
                raise _stop(
                    rules,
                    "needs_input",
                    "riskNeedsReview",
                    "只有权利或安全风险待判断的同类候选，需要复核后继续。",
                    {"category": category, "candidateValues": [item["value"] for item in review_candidates]},
                )
            raise _stop(
                rules,
                "blocked",
                "noCompatibleReplacement",
                "没有通过同类、视觉与权利硬过滤的自主替换值。",
                {"category": category},
            )
        selected = sorted(candidates, key=lambda item: (-float(item["score"]), item["value"]))[0]
    if (
        identity_route is not None
        and identity_route["candidateCardRequired"]
        and not candidate_card_valid(selected)
    ):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "选中的公众人物或知名 IP 替换值没有完整身份候选卡。",
            {"identityRouteRole": identity_route_role},
        )
    identity_text_decisions: list[dict[str, Any]] = []
    if identity_context is not None:
        source_fields = identity_contract["sourceFields"]
        decision_fields = identity_contract["identityTextDecisionFields"]
        actions = identity_contract["identityTextActions"]
        raw_decisions = source_analysis.get(source_fields["textDecisions"])
        required_decision_fields = {
            decision_fields["componentIdentity"],
            decision_fields["sourceText"],
            decision_fields["action"],
            decision_fields["result"],
            decision_fields["basis"],
        }
        optional_evidence_field = decision_fields["highValueEvidence"]
        relationship_field = decision_fields["relationshipType"]
        replacement_identity_field = decision_fields["replacementIdentity"]
        synchronization_fields = {relationship_field, replacement_identity_field}
        relationship_types = identity_contract["identityTextRelationshipTypes"]

        def identity_text_decision_valid(decision: Any) -> bool:
            if not isinstance(decision, dict):
                return False
            action = decision.get(decision_fields["action"])
            component_id = decision.get(decision_fields["componentIdentity"])
            source_value = decision.get(decision_fields["sourceText"])
            result = decision.get(decision_fields["result"])
            closure_item = identity_context["closureByComponent"].get(component_id)
            fields_valid = bool(
                required_decision_fields <= set(decision)
                and set(decision)
                <= required_decision_fields | {optional_evidence_field} | synchronization_fields
                and component_id in identity_context["textComponentIds"]
                and isinstance(source_value, str)
                and source_value.strip()
                and isinstance(closure_item, dict)
                and source_value in closure_item[identity_contract["dependencyFields"]["description"]]
                and action in set(actions.values())
                and isinstance(result, str)
                and isinstance(decision.get(decision_fields["basis"]), str)
                and decision[decision_fields["basis"]].strip()
            )
            if not fields_valid:
                return False
            if action == actions["remove"]:
                return bool(
                    result == ""
                    and optional_evidence_field not in decision
                    and synchronization_fields.isdisjoint(decision)
                )
            if action == actions["synchronize"]:
                relationship = decision.get(relationship_field)
                normalized_result = _normalized_identity(result, identity_modifiers)
                synchronized_result_valid = bool(
                    result.strip()
                    and normalized_result
                    and normalized_result
                    != _normalized_identity(source_value, identity_modifiers)
                    and decision.get(replacement_identity_field) == selected["value"]
                    and relationship in set(relationship_types.values())
                    and optional_evidence_field not in decision
                )
                if relationship == relationship_types["directName"]:
                    synchronized_result_valid = bool(
                        synchronized_result_valid
                        and _normalized_identity(result, identity_modifiers)
                        == _normalized_identity(selected["value"], identity_modifiers)
                    )
                return synchronized_result_valid
            neutral_result = bool(
                result.strip()
                and result not in {source_value, selected["value"]}
                and synchronization_fields.isdisjoint(decision)
            )
            if action == actions["neutralize"]:
                return neutral_result and optional_evidence_field not in decision
            return bool(
                action == actions["exposeNeutralSlot"]
                and neutral_result
                and isinstance(decision.get(optional_evidence_field), str)
                and decision[optional_evidence_field].strip()
            )

        decisions_valid = bool(
            isinstance(raw_decisions, list)
            and len(raw_decisions) == len(identity_context["textComponentIds"])
            and {
                item.get(decision_fields["componentIdentity"])
                for item in raw_decisions
                if isinstance(item, dict)
            }
            == identity_context["textComponentIds"]
            and all(identity_text_decision_valid(item) for item in raw_decisions)
        )
        if not decisions_valid:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "身份文字处理决定未完整覆盖拓扑，或动作、默认结果与依据不一致。",
                {"identityRouteRole": identity_route_role},
            )
        identity_text_decisions = copy.deepcopy(raw_decisions)
        frozen_values = source_analysis.get("frozenSet")
        frozen_evaluation_field = source_fields["frozenConflictEvaluations"]
        raw_frozen_evaluations = source_analysis.get(frozen_evaluation_field)
        frozen_fields = identity_contract["frozenConflictEvaluationFields"]
        required_frozen_fields = set(frozen_fields.values())
        topology_component_ids = set(
            identity_context["topology"][
                identity_contract["topologyFields"]["requiredComponents"]
            ]
        )

        def frozen_evaluation_valid(evaluation: Any) -> bool:
            if not isinstance(evaluation, dict) or set(evaluation) != required_frozen_fields:
                return False
            component_ids = evaluation.get(frozen_fields["componentIdentities"])
            conflict = evaluation.get(frozen_fields["conflict"])
            return bool(
                isinstance(evaluation.get(frozen_fields["frozenValue"]), str)
                and evaluation[frozen_fields["frozenValue"]].strip()
                and isinstance(conflict, bool)
                and isinstance(component_ids, list)
                and all(isinstance(value, str) and value for value in component_ids)
                and len(component_ids) == len(set(component_ids))
                and set(component_ids) <= topology_component_ids
                and conflict is bool(component_ids)
                and isinstance(evaluation.get(frozen_fields["explanation"]), str)
                and evaluation[frozen_fields["explanation"]].strip()
            )

        frozen_evaluations_valid = bool(
            isinstance(frozen_values, list)
            and all(isinstance(value, str) and value.strip() for value in frozen_values)
            and len(frozen_values) == len(set(frozen_values))
            and isinstance(raw_frozen_evaluations, list)
            and len(raw_frozen_evaluations) == len(frozen_values)
            and {
                item.get(frozen_fields["frozenValue"])
                for item in raw_frozen_evaluations
                if isinstance(item, dict)
            }
            == set(frozen_values)
            and all(frozen_evaluation_valid(item) for item in raw_frozen_evaluations)
        )
        if not frozen_evaluations_valid:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "身份路由没有为全部冻结项提供与身份拓扑绑定的冲突证据。",
                {"identityRouteRole": identity_route_role},
            )
        identity_term_keys = {
            _normalized_identity(value, identity_modifiers)
            for value in [
                source_analysis["target"]["identity"],
                selected["value"],
                *[
                    decision[decision_fields["sourceText"]]
                    for decision in identity_text_decisions
                ],
                *[
                    decision[decision_fields["result"]]
                    for decision in identity_text_decisions
                    if decision[decision_fields["result"]]
                ],
            ]
            if isinstance(value, str) and value.strip()
        }
        frozen_identity_conflicts = sorted(
            {
                evaluation[frozen_fields["frozenValue"]]
                for evaluation in raw_frozen_evaluations
                if evaluation[frozen_fields["conflict"]] is True
            }
            | {
                value
                for value in frozen_values
                if any(
                    term in _normalized_identity(value, identity_modifiers)
                    for term in identity_term_keys
                )
            }
        )
        if frozen_identity_conflicts:
            raise _stop(
                rules,
                "blocked",
                "explicitStrategyConflict",
                "身份替换的冻结项与身份拓扑、身份文字或新旧身份发生冲突。",
                {"conflictingValues": frozen_identity_conflicts},
            )
    changed_components = {
        "primary-role": source_analysis["target"]["role"],
        "primary-identity": source_analysis["target"]["identity"],
        **{
            f"dependency-{index}-{item[type_field]}": item[value_field]
            for index, item in enumerate(closure)
        },
    }
    changed_component_ids = set(changed_components)
    changed_values = set(changed_components.values())
    preserve_evaluations = source_analysis.get("preserveConflictEvaluations", [])

    def preserve_evaluation_valid(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        component_ids = item.get("changedComponentIds")
        conflict = item.get("conflictsWithChangedSet")
        preserve_value = item.get("preserveValue")
        return bool(
            isinstance(preserve_value, str)
            and preserve_value
            and isinstance(conflict, bool)
            and isinstance(component_ids, list)
            and all(isinstance(value, str) and value for value in component_ids)
            and len(component_ids) == len(set(component_ids))
            and set(component_ids) <= changed_component_ids
            and conflict is bool(component_ids)
            and (preserve_value not in changed_values or conflict)
        )

    evaluations_valid = (
        isinstance(preserve_evaluations, list)
        and len(preserve_evaluations) == len(preserve_values)
        and {item.get("preserveValue") for item in preserve_evaluations if isinstance(item, dict)}
        == set(preserve_values)
        and all(preserve_evaluation_valid(item) for item in preserve_evaluations)
    )
    if preserve_values and not evaluations_valid:
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源分析没有为全部冻结项提供有效的变更集冲突证据。",
            {"preserveValues": preserve_values},
        )
    preserve_conflicts = sorted(
        set(preserve_values) & changed_values
        | {
            item["preserveValue"]
            for item in preserve_evaluations
            if item["conflictsWithChangedSet"] is True
        }
    )
    if preserve_conflicts:
        raise _stop(
            rules,
            "blocked",
            "explicitStrategyConflict",
            "单图策略要求同时冻结和重绘同一内容，无法安全消解。",
            {"conflictingValues": preserve_conflicts},
        )
    frozen_decision_sources = {
        value: autonomous_source for value in source_analysis["frozenSet"]
    }
    preserve_sources = resolution_value_sources.get(
        strategy_field_roles["preserve"], {}
    )
    frozen_decision_sources.update(
        {
            value: preserve_sources.get(value, per_image_source)
            for value in preserve_values
        }
    )
    plan_fields = identity_contract["planFields"]
    candidate_card_field = identity_contract["candidateFields"]["card"]
    identity_plan_fields = (
        {
            plan_fields["route"]: identity_context["routeEvidence"],
            plan_fields["topology"]: identity_context["topology"],
            plan_fields["textDecisions"]: identity_text_decisions,
            plan_fields["neutralityTerms"]: sorted(
                {
                    source_analysis["target"]["identity"],
                    selected["value"],
                    *(
                        decision[identity_contract["identityTextDecisionFields"]["sourceText"]]
                        for decision in identity_text_decisions
                    ),
                }
            ),
        }
        if identity_context is not None
        else {}
    )
    return {
        "artifactType": "replacement-plan",
        "schemaVersion": rules["schemaVersion"],
        "templateKey": template_key,
        "strategy": strategy,
        "mechanism": source_analysis["mechanism"],
        rules["multiInstanceContract"]["planFields"]["componentGraph"]: component_graph,
        rules["multiInstanceContract"]["planFields"]["imageOperations"]: image_operations,
        "primaryTargets": [
            {
                "sourceCategory": category,
                "sourceRole": source_analysis["target"]["role"],
                "sourceIdentity": source_analysis["target"]["identity"],
                "replacementValue": selected["value"],
                "replacementCategory": selected["category"],
                "reason": selected["reason"],
                "confidence": selected["score"],
                "decisionSource": decision_source,
                **(
                    {candidate_card_field: copy.deepcopy(selected[candidate_card_field])}
                    if identity_route is not None and identity_route["candidateCardRequired"]
                    else {}
                ),
            }
        ],
        "dependencyClosure": [
            {**item, "decisionSource": autonomous_source}
            for item in closure
        ],
        "changedSet": [
            {"kind": "primary", "value": source_analysis["target"]["role"], "decisionSource": decision_source},
            *[
                {
                    "kind": "dependency",
                    "value": item[value_field],
                    "dependencyType": item[type_field],
                    "decisionSource": autonomous_source,
                }
                for item in closure
            ],
        ],
        "frozenSet": list(frozen_decision_sources),
        "frozenSetDecisions": [
            {"value": value, "decisionSource": source}
            for value, source in frozen_decision_sources.items()
        ],
        "replacementPool": candidates,
        "languagePolicy": source_analysis.get("languagePolicy", "preserve_source_language"),
        "rightsReview": "pass",
        "humanReviewRequired": False,
        **identity_plan_fields,
    }
