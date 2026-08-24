from __future__ import annotations

from typing import Any


def author_explicit_slot_suggestion_reviews(
    audit: dict[str, Any],
    content: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Author complete test-only evidence for a deliberately transformed fixture."""

    evidence_field = rules["semanticAuditChecks"]["slotSuggestions"]["evidence"]
    audit["evidence"][evidence_field] = [
        {
            "slotId": slot["id"],
            "defaultValue": slot["defaultValue"],
            "axis": slot["label"],
            "granularity": f"单个{slot['label']}替换值",
            "suggestionReviews": [
                {
                    "value": suggestion,
                    "sameAxis": True,
                    "sameGranularity": True,
                    "mechanismCompatible": True,
                    "evidence": (
                        f"测试作者逐项核对 {suggestion} 与"
                        f" {slot['defaultValue']} 的语义关系"
                    ),
                }
                for suggestion in slot["suggestions"]
            ],
            "evidence": f"测试作者已独立核对 {slot['id']} 的全部推荐值",
        }
        for slot in content["slots"]
    ]
    return audit


def rebuild_rendering_coherence_decision(
    analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    """Keep transformed fixtures explicit about component rendering coverage."""
    rendering = rules["renderingCoherenceDecisionContract"]
    multi = rules["multiInstanceContract"]
    graph_fields = multi["graphFields"]
    component_fields = multi["componentFields"]
    graph = analysis[multi["approvedFields"]["componentGraph"]]
    components = graph[graph_fields["components"]]
    subject_type = rules["slotCompilationContract"]["slotTypes"][
        "primarySubjectUpload"
    ]
    dependent_roles = {
        multi["componentRoles"]["reflection"],
        multi["componentRoles"]["shadow"],
    }
    transfers = []
    for slot in analysis["slotCandidates"]:
        if slot["type"] != subject_type:
            continue
        inheritance = slot.get("identityInheritanceDecision")
        if not isinstance(inheritance, dict):
            continue
        candidates = [
            component
            for component in components
            if component[component_fields["control"]] == slot["id"]
            and component[component_fields["role"]] not in dependent_roles
        ]
        identity_units = {
            component[component_fields["identityUnit"]]
            for component in candidates
        }
        selected = candidates[:1] if len(identity_units) == 1 else candidates
        transfers.append(
            {
                "inputId": slot["id"],
                "targetIds": [
                    component[component_fields["identity"]]
                    for component in selected
                ],
                "inheritFromUpload": inheritance["inheritFromUpload"],
                "keepFromTemplate": inheritance["keepFromTemplate"],
                "renderingUnitId": "whole-approved-image",
                "completeRedraw": True,
                "evidence": (
                    f"输入 {slot['id']} 的身份范围完整重绘进当前确认图的统一媒介"
                ),
            }
        )
    visual = analysis["runtimeSemantics"]["visualContract"]
    analysis[rendering["authoringField"]] = {
        "mode": rendering["modes"]["unified"],
        "approvedImageSha256": analysis["visualFactSourceSha256"],
        "medium": visual["medium"],
        "renderingUnits": [
            {
                "unitId": "whole-approved-image",
                "componentIds": [
                    component[component_fields["identity"]]
                    for component in components
                ],
                "styleTraits": visual["styleTraits"],
                "evidence": (
                    "当前测试确认图中的主体、派生区域、物件与背景共享同一绘制体系"
                ),
            }
        ],
        "boundaryEvidence": [],
        "subjectTransfers": transfers,
        "evidence": "逐组件核对后确认当前测试图采用单一渲染体系",
    }
    return analysis


def rebuild_runtime_targets(
    analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    """Keep transformed test analyses explicit about target role and region."""
    multi = rules["multiInstanceContract"]
    graph_fields = multi["graphFields"]
    component_fields = multi["componentFields"]
    graph = analysis[multi["approvedFields"]["componentGraph"]]
    components = graph[graph_fields["components"]]
    slot_by_id = {slot["id"]: slot for slot in analysis["slotCandidates"]}
    subject_type = rules["slotCompilationContract"]["slotTypes"][
        "primarySubjectUpload"
    ]
    target_kinds = rules["runtimeSemanticsContract"]["targetKinds"]
    dependent_roles = {
        multi["componentRoles"]["reflection"],
        multi["componentRoles"]["shadow"],
    }
    targets = []
    for slot_id, slot in slot_by_id.items():
        controlled = [
            component
            for component in components
            if component[component_fields["control"]] == slot_id
        ]
        is_subject = slot["type"] == subject_type
        if is_subject:
            controlled = [
                component
                for component in controlled
                if component[component_fields["role"]] not in dependent_roles
            ]
            identity_units = {
                component[component_fields["identityUnit"]]
                for component in controlled
            }
            if len(identity_units) == 1:
                controlled = controlled[:1]
        kind = (
            target_kinds["identitySubject"]
            if is_subject
            else target_kinds["contentElement"]
        )
        for component in controlled:
            component_id = component[component_fields["identity"]]
            targets.append(
                {
                    "id": component_id,
                    "kind": kind,
                    "role": f"{slot['label']}的测试可见实例",
                    "region": f"测试画面中由 {slot_id} 控制的 {component_id} 空间区域",
                }
            )
    analysis.setdefault("runtimeSemantics", {})["targetInstances"] = targets
    return analysis


def rebuild_source_component_graph_for_named_closure(
    analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    """Bind an identity fixture's named closure to one image operation."""

    identity_contract = rules["identityReplacementContract"]
    dependency_fields = identity_contract["dependencyFields"]
    dependency_types = identity_contract["dependencyTypes"]
    component_field = dependency_fields["componentIdentity"]
    type_field = dependency_fields["dependencyType"]
    closure = analysis["dependencyClosure"]
    if not closure or not all(
        isinstance(item.get(component_field), str) and item[component_field]
        for item in closure
    ):
        return analysis

    contract = rules["multiInstanceContract"]
    graph_fields = contract["graphFields"]
    component_fields = contract["componentFields"]
    relation_fields = contract["relationFields"]
    operation_fields = contract["operationFields"]
    role_by_dependency_type = {
        dependency_types["fullBody"]: contract["componentRoles"]["subject"],
        dependency_types["repeatedInstance"]: contract["componentRoles"]["subject"],
        dependency_types["shadow"]: contract["componentRoles"]["shadow"],
        dependency_types["reflection"]: contract["componentRoles"]["reflection"],
        dependency_types["framedPortrait"]: contract["componentRoles"]["reflection"],
        dependency_types["identityText"]: contract["componentRoles"]["text"],
        dependency_types["identityBadge"]: contract["componentRoles"]["prop"],
        dependency_types["identityMark"]: contract["componentRoles"]["prop"],
        dependency_types["contactBoundary"]: contract["componentRoles"]["prop"],
    }
    visual_types = {
        dependency_types["fullBody"],
        dependency_types["repeatedInstance"],
        dependency_types["reflection"],
        dependency_types["framedPortrait"],
    }
    identity_closure = all(item[type_field] in role_by_dependency_type for item in closure)
    category = analysis["target"]["category"]
    categories = rules["sourceCategories"]
    if identity_closure:
        operation_role = "identityReplace"
        primary_id = next(
            item[component_field]
            for item in closure
            if item[type_field] == dependency_types["fullBody"]
        )
        components = [
            {
                component_fields["identity"]: item[component_field],
                component_fields["role"]: role_by_dependency_type[item[type_field]],
                component_fields["identityUnit"]: "fixture-source-identity",
                component_fields["visualInstance"]: item[type_field] in visual_types,
                component_fields["uploadAsset"]: None,
                component_fields["control"]: None,
                component_fields["container"]: None,
                component_fields["explanation"]: "依赖闭包组件与来源画面区域逐项绑定",
            }
            for item in closure
        ]
        stable_ids = ("fixture-stable-container", "fixture-stable-background")
        stable_roles = ("container", "background")
    else:
        scene_route = category == categories["sceneAttribute"]
        mask_route = category in {
            categories["textContent"],
            categories["genericObject"],
            categories["genericFood"],
        }
        operation_role = "sceneReplace" if scene_route else "maskFill" if mask_route else "identityReplace"
        target_role = (
            "background"
            if scene_route
            else "text"
            if category == categories["textContent"]
            else "prop"
            if mask_route
            else "subject"
        )
        target_has_identity = operation_role == "identityReplace"
        components = [
            {
                component_fields["identity"]: item[component_field],
                component_fields["role"]: contract["componentRoles"][target_role],
                component_fields["identityUnit"]: (
                    "fixture-source-identity" if target_has_identity else None
                ),
                component_fields["visualInstance"]: target_has_identity,
                component_fields["uploadAsset"]: None,
                component_fields["control"]: None,
                component_fields["container"]: None,
                component_fields["explanation"]: "具名依赖闭包与单图目标区域逐项绑定",
            }
            for item in closure
        ]
        primary_id = closure[0][component_field]
        stable_ids = ("fixture-stable-subject", "fixture-stable-background")
        stable_roles = ("subject", "background")
    components.extend(
        {
            component_fields["identity"]: component_id,
            component_fields["role"]: contract["componentRoles"][role],
            component_fields["identityUnit"]: (
                "fixture-stable-identity" if role == "subject" else None
            ),
            component_fields["visualInstance"]: role == "subject",
            component_fields["uploadAsset"]: None,
            component_fields["control"]: None,
            component_fields["container"]: None,
            component_fields["explanation"]: "替换操作之外保持稳定的来源锚点",
        }
        for component_id, role in zip(stable_ids, stable_roles)
    )
    relation_role_by_dependency_type = {
        dependency_types["repeatedInstance"]: "repeatedIdentity",
        dependency_types["shadow"]: "shadow",
        dependency_types["reflection"]: "reflection",
        dependency_types["framedPortrait"]: "reflection",
        dependency_types["contactBoundary"]: "contact",
    }
    relations = []
    preserved_relation_ids = []
    for index, item in enumerate(closure if identity_closure else []):
        relation_role = relation_role_by_dependency_type.get(item[type_field])
        if relation_role is None:
            continue
        relation_id = f"fixture-relation-{index}"
        contact_boundary = item[type_field] == dependency_types["contactBoundary"]
        relations.append(
            {
                relation_fields["identity"]: relation_id,
                relation_fields["type"]: contract["relationTypes"][relation_role],
                relation_fields["source"]: primary_id if contact_boundary else item[component_field],
                relation_fields["target"]: item[component_field] if contact_boundary else primary_id,
                relation_fields["explanation"]: "依赖组件与主体的结构关系",
            }
        )
        if contact_boundary:
            preserved_relation_ids.append(relation_id)

    analysis[contract["sourceFields"]["componentGraph"]] = {
        graph_fields["components"]: components,
        graph_fields["relations"]: relations,
        graph_fields["explanation"]: "身份依赖闭包、派生实例和稳定锚点的来源组件图",
    }
    existing_operations = analysis.get(contract["sourceFields"]["imageOperations"], [])
    operation_id = (
        existing_operations[0].get(operation_fields["identity"])
        if isinstance(existing_operations, list)
        and existing_operations
        and isinstance(existing_operations[0], dict)
        else None
    ) or "fixture-identity-replacement"
    analysis[contract["sourceFields"]["imageOperations"]] = [
        {
            operation_fields["identity"]: operation_id,
            operation_fields["operation"]: contract["operations"][operation_role],
            operation_fields["targetRegions"]: [item[component_field] for item in closure],
            operation_fields["clearRequirements"]: ["清除全部旧身份与派生区域残留"],
            operation_fields["stableAnchors"]: list(stable_ids),
            operation_fields["preservedRelations"]: preserved_relation_ids,
            operation_fields["explanation"]: "闭包内组件统一替换，闭包外锚点保持稳定",
        }
    ]
    return analysis


def rebuild_approved_component_graph(
    analysis: dict[str, Any],
    rules: dict[str, Any],
    source_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build deterministic component evidence from a transformed fixture analysis."""

    contract = rules["multiInstanceContract"]
    graph_fields = contract["graphFields"]
    component_fields = contract["componentFields"]
    approved_graph_field = contract["approvedFields"]["componentGraph"]
    existing_graph = analysis.get(approved_graph_field, {})
    existing_components = existing_graph.get(graph_fields["components"], [])
    existing_component_by_id = {
        component[component_fields["identity"]]: component
        for component in existing_components
        if isinstance(component, dict)
        and isinstance(component.get(component_fields["identity"]), str)
    }
    rendering_contract = rules.get("renderingCoherenceDecisionContract", {})
    rendering_decision = analysis.get(rendering_contract.get("authoringField"))
    rendering_fields = rendering_contract.get("fields", {})
    rendering_unit_fields = rendering_contract.get("renderingUnitFields", {})
    existing_rendering_units = (
        rendering_decision.get(rendering_fields.get("renderingUnits"), [])
        if isinstance(rendering_decision, dict)
        else []
    )
    existing_rendering_component_ids = {
        component_id
        for unit in existing_rendering_units
        if isinstance(unit, dict)
        for component_id in unit.get(
            rendering_unit_fields.get("componentIdentities"), []
        )
    }
    existing_bindings = analysis.get(
        contract["approvedFields"]["operationBindings"], []
    )
    count_fields = rules["slotCompilationContract"]["assetUnitCountFields"]
    slot_contract = rules["slotCompilationContract"]
    counts = analysis["assetUnitAnalysis"]
    control_ids = [slot["id"] for slot in analysis["slotCandidates"]]
    visible_count = counts[count_fields["visibleSubjects"]]
    identity_count = counts[count_fields["identities"]]
    upload_count = counts[count_fields["uploads"]]
    components = []
    slot_by_id = {slot["id"]: slot for slot in analysis["slotCandidates"]}

    def component_role_for_slot(slot: dict[str, Any]) -> str:
        semantic_role = slot.get("semanticRole")
        if semantic_role == slot_contract["semanticRoles"]["primarySubject"]:
            return "subject"
        if semantic_role in {
            slot_contract["semanticRoles"]["sceneContent"],
            slot_contract["semanticRoles"]["sceneAtmosphere"],
        }:
            return "background"
        if semantic_role in {
            slot_contract["semanticRoles"]["identityText"],
            slot_contract["semanticRoles"]["primaryVisualText"],
            slot_contract["semanticRoles"]["highValueTextSpan"],
            slot_contract["semanticRoles"]["decorativeCaption"],
        }:
            return "text"
        if semantic_role == slot_contract["semanticRoles"]["containerAppearance"]:
            return "container"
        if semantic_role in set(slot_contract["personAttributeRoles"].values()):
            return "subject"
        return "prop"

    def new_component(
        index: int,
        role: str,
        *,
        control_id: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            component_fields["identity"]: f"fixture-component-{index}",
            component_fields["role"]: contract["componentRoles"][role],
            component_fields["identityUnit"]: None,
            component_fields["visualInstance"]: False,
            component_fields["uploadAsset"]: upload_id,
            component_fields["control"]: control_id,
            component_fields["container"]: None,
            component_fields["explanation"]: "根据测试场景独立重建实例、身份、上传素材和控件证据",
        }

    for slot in analysis["slotCandidates"]:
        components.append(
            new_component(
                len(components),
                component_role_for_slot(slot),
                control_id=slot["id"],
            )
        )

    subject_upload_type = slot_contract["slotTypes"]["primarySubjectUpload"]
    upload_controls = [
        slot["id"]
        for slot in analysis["slotCandidates"]
        if slot["type"] == subject_upload_type
    ]
    for index in range(upload_count):
        control_id = upload_controls[index % len(upload_controls)] if upload_controls else None
        existing = next(
            (
                component
                for component in components
                if component[component_fields["control"]] == control_id
                and component[component_fields["uploadAsset"]] is None
            ),
            None,
        )
        if existing is None:
            role = (
                component_role_for_slot(slot_by_id[control_id])
                if control_id is not None
                else "subject"
            )
            existing = new_component(
                len(components), role, control_id=control_id
            )
            components.append(existing)
        existing[component_fields["uploadAsset"]] = f"fixture-upload-{index}"

    primary_semantic_role = slot_contract["semanticRoles"]["primarySubject"]
    visual_candidates = [
        component
        for component in components
        if component[component_fields["control"]] is not None
        and slot_by_id[component[component_fields["control"]]]["semanticRole"]
        == primary_semantic_role
    ]
    while len(visual_candidates) < visible_count:
        component = new_component(len(components), "subject")
        components.append(component)
        visual_candidates.append(component)
    for component in visual_candidates[:visible_count]:
        component[component_fields["visualInstance"]] = True

    identity_candidates = [
        *visual_candidates[:visible_count],
        *[
            component
            for component in components
            if component not in visual_candidates[:visible_count]
            and component[component_fields["role"]]
            == contract["componentRoles"]["subject"]
        ],
    ]
    while len(identity_candidates) < identity_count:
        component = new_component(len(components), "subject")
        components.append(component)
        identity_candidates.append(component)
    for index, component in enumerate(identity_candidates[:identity_count]):
        component[component_fields["identityUnit"]] = f"fixture-identity-{index}"
    if identity_count > 0:
        for component in visual_candidates[:visible_count]:
            if component[component_fields["identityUnit"]] is None:
                component[component_fields["identityUnit"]] = "fixture-identity-0"
    relations = []
    if identity_count > 0:
        primary_component = next(
            (
                component
                for component in components
                if component[component_fields["identityUnit"]] is not None
            ),
            None,
        )
        derived_roles = {
            contract["componentRoles"]["reflection"],
            contract["componentRoles"]["shadow"],
        }
        derived_components = [
            component
            for component in existing_components
            if component.get(component_fields["role"]) in derived_roles
            or (
                component.get(component_fields["role"])
                == contract["componentRoles"]["prop"]
                and component.get(component_fields["identityUnit"]) is not None
                and component.get(component_fields["control"]) is None
            )
        ]
        for index, original in enumerate(derived_components):
            component_id = f"fixture-derived-component-{index}"
            role = original[component_fields["role"]]
            components.append(
                {
                    component_fields["identity"]: component_id,
                    component_fields["role"]: role,
                    component_fields["identityUnit"]: (
                        primary_component[component_fields["identityUnit"]]
                        if primary_component is not None
                        else "fixture-identity-0"
                    ),
                    component_fields["visualInstance"]: False,
                    component_fields["uploadAsset"]: None,
                    component_fields["control"]: None,
                    component_fields["container"]: None,
                    component_fields["explanation"]: "保留确认模板图的身份派生组件拓扑",
                }
            )
            relation_role = (
                "reflection"
                if role == contract["componentRoles"]["reflection"]
                else "shadow"
                if role == contract["componentRoles"]["shadow"]
                else None
            )
            if relation_role is not None and primary_component is not None:
                relations.append(
                    {
                        contract["relationFields"]["identity"]: f"fixture-derived-relation-{index}",
                        contract["relationFields"]["type"]: contract["relationTypes"][relation_role],
                        contract["relationFields"]["source"]: component_id,
                        contract["relationFields"]["target"]: primary_component[component_fields["identity"]],
                        contract["relationFields"]["explanation"]: "派生组件与确认主体属于同一身份",
                    }
                )
        if primary_component is not None:
            stable_container = next(
                (
                    component
                    for component in components
                    if component[component_fields["role"]]
                    == contract["componentRoles"]["container"]
                ),
                None,
            )
            if stable_container is None:
                stable_container = {
                    component_fields["identity"]: "fixture-approved-container",
                    component_fields["role"]: contract["componentRoles"]["container"],
                    component_fields["identityUnit"]: None,
                    component_fields["visualInstance"]: False,
                    component_fields["uploadAsset"]: None,
                    component_fields["control"]: None,
                    component_fields["container"]: None,
                    component_fields["explanation"]: "确认模板图中的稳定承托容器",
                }
                components.append(stable_container)
            relations.append(
                {
                    contract["relationFields"]["identity"]: "fixture-approved-contact",
                    contract["relationFields"]["type"]: contract["relationTypes"]["contact"],
                    contract["relationFields"]["source"]: primary_component[component_fields["identity"]],
                    contract["relationFields"]["target"]: stable_container[component_fields["identity"]],
                    contract["relationFields"]["explanation"]: "确认主体与稳定容器的接触边界",
                }
            )
    analysis[approved_graph_field] = {
        graph_fields["components"]: components,
        graph_fields["relations"]: relations,
        graph_fields["explanation"]: "确定性 fixture 按已声明四类数量重建组件图",
    }
    if source_analysis is not None:
        source_graph = source_analysis[contract["sourceFields"]["componentGraph"]]
        source_components = {
            component[component_fields["identity"]]: component
            for component in source_graph[graph_fields["components"]]
        }
        operation_fields = contract["operationFields"]
        target_components = [
            source_components[target_id]
            for operation in source_analysis[contract["sourceFields"]["imageOperations"]]
            for target_id in operation[operation_fields["targetRegions"]]
        ]
        scoped_components = [
            *target_components,
            *[
                source_components[anchor_id]
                for operation in source_analysis[
                    contract["sourceFields"]["imageOperations"]
                ]
                for anchor_id in operation[operation_fields["stableAnchors"]]
            ],
        ]
        required_by_role: dict[str, int] = {}
        for component in scoped_components:
            role = component[component_fields["role"]]
            required_by_role[role] = required_by_role.get(role, 0) + 1
        observed_by_role: dict[str, int] = {}
        for component in components:
            role = component[component_fields["role"]]
            observed_by_role[role] = observed_by_role.get(role, 0) + 1
        primary = next(
            (
                component
                for component in components
                if component[component_fields["role"]]
                == contract["componentRoles"]["subject"]
                and component[component_fields["identityUnit"]] is not None
            ),
            None,
        )
        for role, required_count in required_by_role.items():
            matching_source = next(
                component
                for component in scoped_components
                if component[component_fields["role"]] == role
            )
            for _ in range(max(0, required_count - observed_by_role.get(role, 0))):
                component_id = f"fixture-operation-component-{len(components)}"
                identity_unit = (
                    primary[component_fields["identityUnit"]]
                    if matching_source[component_fields["identityUnit"]] is not None
                    and primary is not None
                    else None
                )
                components.append(
                    {
                        component_fields["identity"]: component_id,
                        component_fields["role"]: role,
                        component_fields["identityUnit"]: identity_unit,
                        component_fields["visualInstance"]: False,
                        component_fields["uploadAsset"]: None,
                        component_fields["control"]: None,
                        component_fields["container"]: None,
                        component_fields["explanation"]: "根据来源图片操作补齐确认组件角色",
                    }
                )
                relation_role = (
                    "repeatedIdentity"
                    if role == contract["componentRoles"]["subject"]
                    else "reflection"
                    if role == contract["componentRoles"]["reflection"]
                    else "shadow"
                    if role == contract["componentRoles"]["shadow"]
                    else None
                )
                if relation_role is not None and primary is not None:
                    relations.append(
                        {
                            contract["relationFields"]["identity"]: f"fixture-operation-relation-{len(relations)}",
                            contract["relationFields"]["type"]: contract["relationTypes"][relation_role],
                            contract["relationFields"]["source"]: component_id,
                            contract["relationFields"]["target"]: primary[component_fields["identity"]],
                            contract["relationFields"]["explanation"]: "确认派生组件与主体的身份关系",
                        }
                    )
                observed_by_role[role] = observed_by_role.get(role, 0) + 1

        for operation in source_analysis[contract["sourceFields"]["imageOperations"]]:
            if (
                operation[operation_fields["operation"]]
                != contract["operations"]["identityReplace"]
            ):
                continue
            required_roles = [
                source_components[target_id][component_fields["role"]]
                for target_id in operation[operation_fields["targetRegions"]]
            ]
            primary_identity = next(
                (
                    component[component_fields["identityUnit"]]
                    for component in components
                    if component[component_fields["identityUnit"]] is not None
                ),
                None,
            )
            if primary_identity is None:
                continue
            selected_identity_component_ids: set[str] = set()
            for role in set(required_roles):
                candidates = [
                    component
                    for component in components
                    if component[component_fields["role"]] == role
                    and component[component_fields["identityUnit"]]
                    == primary_identity
                ]
                missing = required_roles.count(role) - len(candidates)
                if missing > 0:
                    unbound = [
                        component
                        for component in components
                        if component[component_fields["role"]] == role
                        and component[component_fields["identityUnit"]] is None
                    ][:missing]
                    for component in unbound:
                        component[component_fields["identityUnit"]] = primary_identity
                    candidates.extend(unbound)
                selected_identity_component_ids.update(
                    component[component_fields["identity"]]
                    for component in sorted(
                        candidates,
                        key=lambda value: value[component_fields["control"]] is None,
                    )[: required_roles.count(role)]
                )
            removed_ids = {
                component[component_fields["identity"]]
                for component in components
                if component[component_fields["identityUnit"]] == primary_identity
                and component[component_fields["identity"]]
                not in selected_identity_component_ids
            }
            components[:] = [
                component
                for component in components
                if component[component_fields["identity"]] not in removed_ids
            ]
            relations[:] = [
                relation
                for relation in relations
                if relation[contract["relationFields"]["source"]] not in removed_ids
                and relation[contract["relationFields"]["target"]] not in removed_ids
            ]

        primary_subject_role = slot_contract["semanticRoles"]["primarySubject"]
        for operation in source_analysis[contract["sourceFields"]["imageOperations"]]:
            operation_role = next(
                role
                for role, value in contract["operations"].items()
                if value == operation[operation_fields["operation"]]
            )
            if operation_role == "identityReplace":
                continue
            target_roles = {
                source_components[target_id][component_fields["role"]]
                for target_id in operation[operation_fields["targetRegions"]]
            }
            for target_role in target_roles:
                if any(
                    component[component_fields["role"]] == target_role
                    and component[component_fields["control"]] is not None
                    for component in components
                ):
                    continue
                target_component = next(
                    (
                        component
                        for component in components
                        if component[component_fields["role"]] == target_role
                        and component[component_fields["control"]] is None
                    ),
                    None,
                )
                donor = next(
                    (
                        component
                        for component in components
                        if component[component_fields["control"]] in slot_by_id
                        and slot_by_id[component[component_fields["control"]]][
                            "semanticRole"
                        ]
                        == primary_subject_role
                    ),
                    None,
                )
                if target_component is not None and donor is not None:
                    target_component[component_fields["control"]] = donor[
                        component_fields["control"]
                    ]
                    target_component[component_fields["uploadAsset"]] = donor[
                        component_fields["uploadAsset"]
                    ]
                    donor[component_fields["control"]] = None
                    donor[component_fields["uploadAsset"]] = None

    binding_fields = contract["approvedOperationBindingFields"]
    if source_analysis is not None:
        source_graph = source_analysis[contract["sourceFields"]["componentGraph"]]
        source_component_by_id = {
            component[component_fields["identity"]]: component
            for component in source_graph[graph_fields["components"]]
        }
        binding_specs = [
            {
                "specOperationIdentity": operation[operation_fields["identity"]],
                "operation": operation[operation_fields["operation"]],
                "targetRoles": [
                    source_component_by_id[component_id][component_fields["role"]]
                    for component_id in operation[operation_fields["targetRegions"]]
                ],
                "anchorRoles": [
                    source_component_by_id[component_id][component_fields["role"]]
                    for component_id in operation[operation_fields["stableAnchors"]]
                ],
            }
            for operation in source_analysis[contract["sourceFields"]["imageOperations"]]
        ]
    else:
        binding_specs = []
        for binding in existing_bindings:
            target_components = [
                existing_component_by_id[component_id]
                for component_id in binding[binding_fields["targetComponents"]]
                if component_id in existing_component_by_id
            ]
            target_identity_units = {
                component[component_fields["identityUnit"]]
                for component in target_components
            }
            operation_value = (
                contract["operations"]["identityReplace"]
                if target_identity_units
                and None not in target_identity_units
                and len(target_identity_units) == 1
                else None
            )
            binding_specs.append(
                {
                    "specOperationIdentity": binding[binding_fields["operationIdentity"]],
                    "operation": operation_value,
                    "targetRoles": [
                        component[component_fields["role"]]
                        for component in target_components
                    ],
                    "anchorRoles": [
                        existing_component_by_id[component_id][component_fields["role"]]
                        for component_id in binding[binding_fields["stableAnchors"]]
                        if component_id in existing_component_by_id
                    ],
                }
            )

    if source_analysis is None:
        primary_identity_component = next(
            (
                component
                for component in components
                if component[component_fields["identityUnit"]] is not None
            ),
            None,
        )
        for spec in binding_specs:
            required_roles = [*spec["targetRoles"], *spec["anchorRoles"]]
            observed_roles = [
                component[component_fields["role"]] for component in components
            ]
            for role in set(required_roles):
                for _ in range(
                    max(0, required_roles.count(role) - observed_roles.count(role))
                ):
                    component = new_component(len(components), "subject")
                    component[component_fields["role"]] = role
                    if (
                        spec["operation"]
                        == contract["operations"]["identityReplace"]
                        and role in spec["targetRoles"]
                        and primary_identity_component is not None
                    ):
                        component[component_fields["identityUnit"]] = (
                            primary_identity_component[
                                component_fields["identityUnit"]
                            ]
                        )
                    components.append(component)
                    observed_roles.append(role)

    approved_bindings = []
    used_component_ids: set[str] = set()
    for spec in binding_specs:
        target_candidates = components
        if spec["operation"] == contract["operations"]["identityReplace"]:
            required_roles = spec["targetRoles"]
            identity_units = {
                component[component_fields["identityUnit"]]
                for component in components
                if component[component_fields["identityUnit"]] is not None
            }
            matching_groups = []
            for identity_unit in identity_units:
                group = [
                    component
                    for component in components
                    if component[component_fields["identityUnit"]] == identity_unit
                ]
                observed = [component[component_fields["role"]] for component in group]
                if all(observed.count(role) >= required_roles.count(role) for role in set(required_roles)):
                    matching_groups.append(group)
            if len(matching_groups) == 1:
                target_candidates = matching_groups[0]

        def select_components(role_values: list[str]) -> list[dict[str, Any]]:
            selected = []
            locally_used: set[str] = set()
            for role in role_values:
                component = next(
                    item
                    for item in sorted(
                        target_candidates,
                        key=lambda value: value[component_fields["control"]] is None,
                    )
                    if item[component_fields["role"]] == role
                    and item[component_fields["identity"]] not in used_component_ids
                    and item[component_fields["identity"]] not in locally_used
                )
                selected.append(component)
                locally_used.add(component[component_fields["identity"]])
            used_component_ids.update(locally_used)
            return selected

        selected_targets = select_components(spec["targetRoles"])
        target_candidates = components
        selected_anchors = select_components(spec["anchorRoles"])
        approved_bindings.append(
            {
                binding_fields["operationIdentity"]: spec["specOperationIdentity"],
                binding_fields["targetComponents"]: [
                    component[component_fields["identity"]]
                    for component in selected_targets
                ],
                binding_fields["stableAnchors"]: [
                    component[component_fields["identity"]]
                    for component in selected_anchors
                ],
                binding_fields["controls"]: sorted(
                    {
                        component[component_fields["control"]]
                        for component in selected_targets
                        if component[component_fields["control"]] is not None
                    }
                ),
                binding_fields["explanation"]: "确认模板图目标、稳定锚点与用户控件已显式绑定",
            }
        )
    analysis[contract["approvedFields"]["operationBindings"]] = approved_bindings
    analysis = rebuild_runtime_targets(analysis, rules)
    if (
        isinstance(rendering_decision, dict)
        and len(existing_rendering_units) == 1
        and existing_rendering_component_ids == set(existing_component_by_id)
    ):
        existing_rendering_units[0][
            rendering_unit_fields["componentIdentities"]
        ] = [component[component_fields["identity"]] for component in components]
        transfer_fields = rendering_contract["subjectTransferFields"]
        dependent_roles = {
            contract["componentRoles"]["reflection"],
            contract["componentRoles"]["shadow"],
        }
        for transfer in rendering_decision[rendering_fields["subjectTransfers"]]:
            slot_id = transfer[transfer_fields["inputIdentity"]]
            candidates = [
                component
                for component in components
                if component[component_fields["control"]] == slot_id
                and component[component_fields["role"]] not in dependent_roles
            ]
            identity_units = {
                component[component_fields["identityUnit"]]
                for component in candidates
            }
            selected = candidates[:1] if len(identity_units) == 1 else candidates
            transfer[transfer_fields["targetIdentities"]] = [
                component[component_fields["identity"]] for component in selected
            ]
    return analysis
