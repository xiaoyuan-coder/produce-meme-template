from __future__ import annotations

import copy
from typing import Any


def compile_critical_outcome_qualification(
    generation_package: dict[str, Any],
    visual_review: dict[str, Any],
    authoring_audit: dict[str, Any],
    validation_report: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Compile one fail-closed ledger for the workflow's critical outcomes."""

    contract = rules["criticalOutcomeContract"]
    fields = contract["fields"]
    result_fields = contract["requirementResultFields"]
    requirement_ids = contract["requirementIds"]
    stage_one_contract = rules["sourceAuthoringContextContract"][
        "firstStageGateContract"
    ]
    stage_one = generation_package[stage_one_contract["field"]][
        stage_one_contract["fields"]["requirementResults"]
    ]
    stage_one_by_id = {
        item[result_fields["identity"]]: item
        for item in stage_one
        if isinstance(item, dict)
    }

    visual_contract = rules["visualReviewContract"]
    visual_fields = visual_contract["evidenceFieldRoles"]
    interaction_fields = visual_contract["interactionIntegrityFields"]
    interaction_evidence = visual_review.get(
        visual_fields["interactionIntegrity"], []
    )
    interaction_pass = bool(
        isinstance(interaction_evidence, list)
        and all(
            isinstance(item, dict)
            and all(
                item.get(interaction_fields[role]) is True
                for role in (
                    "subjectPartsTraceable",
                    "topologyPlausible",
                    "contactPlausible",
                    "occlusionOrderPlausible",
                    "noFusionOrExtraParts",
                )
            )
            for item in interaction_evidence
        )
    )

    audit_contract = rules["authoringContractAudit"]
    audit_fields = audit_contract["reviewFields"]
    copy_fields = audit_contract["copyReviewFields"]
    prompt_fields = audit_contract["promptReviewFields"]
    slot_fields = audit_contract["slotReviewFields"]
    inheritance_fields = audit_contract["identityInheritanceReviewFields"]
    default_fields = audit_contract["defaultValueReviewFields"]
    tag_fields = audit_contract["tagReviewFields"]
    subject_fields = audit_contract["subjectPresenceReviewFields"]
    copy_review = authoring_audit.get(audit_fields["copyReview"], {})
    copy_pass = bool(
        isinstance(copy_review, dict)
        and all(
            copy_review.get(copy_fields[role]) is True
            for role in (
                "titleGrounded",
                "descriptionGrounded",
                "spokenNaturalness",
                "slotPortability",
            )
        )
    )
    prompt_review = authoring_audit.get(audit_fields["promptReview"], {})
    prompt_pass = bool(
        isinstance(prompt_review, dict)
        and prompt_review.get(prompt_fields["userEditableOnly"]) is True
        and prompt_review.get(prompt_fields["leakedProductionClauses"]) == []
    )
    slot_reviews = authoring_audit.get(audit_fields["slotReviews"], [])
    slot_gate_roles = (
        "userMotivation",
        "visuallyVisible",
        "modelControllable",
        "mechanismPreserved",
    )
    slot_pass = bool(
        isinstance(slot_reviews, list)
        and slot_reviews
        and all(
            isinstance(item, dict)
            and item.get(slot_fields["componentIdentities"])
            and all(item.get(slot_fields[role]) is True for role in slot_gate_roles)
            for item in slot_reviews
        )
    )
    inheritance_reviews = authoring_audit.get(
        audit_fields["identityInheritanceReviews"], []
    )
    inheritance_pass = bool(
        isinstance(inheritance_reviews, list)
        and all(
            isinstance(item, dict)
            and all(
                item.get(inheritance_fields[role]) is True
                for role in (
                    "uploadTraitsComplete",
                    "templateExceptionsMinimal",
                    "clothingPolicyValid",
                )
            )
            for item in inheritance_reviews
        )
    )
    default_reviews = authoring_audit.get(audit_fields["defaultValueReviews"], [])
    default_pass = bool(
        isinstance(default_reviews, list)
        and default_reviews
        and all(
            isinstance(item, dict)
            and all(
                item.get(default_fields[role]) is True
                for role in ("userFacing", "singleAxis", "minimalWording")
            )
            for item in default_reviews
        )
    )
    subject_review = authoring_audit.get(
        audit_fields["subjectPresenceReview"], {}
    )
    subject_policy_pass = bool(
        isinstance(subject_review, dict)
        and all(
            subject_review.get(subject_fields[role]) is True
            for role in (
                "declarationMatchesImage",
                "graphMatchesImage",
                "handoffMatchesImage",
                "slotPolicyValid",
            )
        )
    )
    tag_reviews = authoring_audit.get(audit_fields["tagReviews"], [])
    tag_pass = bool(
        isinstance(tag_reviews, list)
        and tag_reviews
        and all(
            isinstance(item, dict)
            and item.get(tag_fields["groundedInApprovedImage"]) is True
            and item.get(tag_fields["classificationUseful"]) is True
            for item in tag_reviews
        )
    )

    layers = validation_report.get("layers", {})
    schema_layer = layers.get("schema", {}) if isinstance(layers, dict) else {}
    semantic_layer = layers.get("semantic", {}) if isinstance(layers, dict) else {}
    visual_layer = (
        layers.get("visualContract", {}) if isinstance(layers, dict) else {}
    )
    gallery_layer = (
        layers.get("galleryContract", {}) if isinstance(layers, dict) else {}
    )
    semantic_evidence = (
        semantic_layer.get("evidence", {})
        if isinstance(semantic_layer, dict)
        else {}
    )
    semantic_audit_evidence = semantic_evidence.get("semanticAudit", {})
    semantic_checks = (
        semantic_audit_evidence.get("checks", {})
        if isinstance(semantic_audit_evidence, dict)
        else {}
    )

    def semantic_check_pass(role: str) -> bool:
        check = rules["semanticAuditChecks"][role]["check"]
        evidence_name = rules["semanticAuditChecks"][role]["evidence"]
        evidence_values = (
            semantic_audit_evidence.get("evidence", {})
            if isinstance(semantic_audit_evidence, dict)
            else {}
        )
        return bool(
            semantic_layer.get("pass") is True
            and isinstance(semantic_checks, dict)
            and semantic_checks.get(check) is True
            and isinstance(evidence_values, dict)
            and evidence_name in evidence_values
        )
    grounding_field = rules["visualContractGroundingReviewContract"][
        "evidenceField"
    ]
    grounding_review = (
        semantic_audit_evidence.get("evidence", {}).get(grounding_field)
        if isinstance(semantic_audit_evidence, dict)
        and isinstance(semantic_audit_evidence.get("evidence"), dict)
        else None
    )
    grounding_fields = rules["visualContractGroundingReviewContract"][
        "reviewFields"
    ]
    visible_text_field = rules["semanticAuditChecks"]["visibleTextClassification"][
        "evidence"
    ]
    visible_text_review = (
        semantic_audit_evidence.get("evidence", {}).get(visible_text_field)
        if isinstance(semantic_audit_evidence, dict)
        and isinstance(semantic_audit_evidence.get("evidence"), dict)
        else None
    )
    visible_text_fields = rules["visibleTextContract"]["semanticAuditFields"]
    visible_text_pass = bool(
        semantic_layer.get("pass") is True
        and isinstance(visible_text_review, dict)
        and visible_text_review.get(visible_text_fields["complete"]) is True
        and visible_text_review.get(visible_text_fields["fixedRegionLeaks"]) == []
        and isinstance(
            visible_text_review.get(visible_text_fields["reviewedRegionIdentities"]),
            list,
        )
        and isinstance(visible_text_review.get(visible_text_fields["decisions"]), list)
    )
    runtime_style_pass = bool(
        semantic_layer.get("pass") is True
        and isinstance(grounding_review, dict)
        and all(
            grounding_review.get(grounding_fields[role]) is True
            for role in (
                "mediumMatchesApprovedImage",
                "compositionMatchesApprovedImage",
                "relationsMatchApprovedImage",
            )
        )
    )
    unit_review_fields = rules["visualContractGroundingReviewContract"][
        "renderingUnitReviewFields"
    ]
    transfer_review_fields = rules["visualContractGroundingReviewContract"][
        "subjectTransferReviewFields"
    ]
    unit_reviews = (
        grounding_review.get(grounding_fields["renderingUnitReviews"])
        if isinstance(grounding_review, dict)
        else None
    )
    transfer_reviews = (
        grounding_review.get(grounding_fields["subjectTransferReviews"])
        if isinstance(grounding_review, dict)
        else None
    )
    rendering_coherence_pass = bool(
        runtime_style_pass
        and isinstance(unit_reviews, list)
        and unit_reviews
        and all(
            isinstance(item, dict)
            and item.get(unit_review_fields["matchesApprovedImage"]) is True
            for item in unit_reviews
        )
        and isinstance(transfer_reviews, list)
        and all(
            isinstance(item, dict)
            and item.get(transfer_review_fields["completeRedraw"]) is True
            and item.get(transfer_review_fields["authorityMatches"]) is True
            for item in transfer_reviews
        )
    )
    approved_image_quality_pass = visual_layer.get("pass") is True
    formal_record_pass = bool(
        schema_layer.get("pass") is True and gallery_layer.get("pass") is True
    )

    results = [
        copy.deepcopy(stage_one_by_id[requirement_ids[role]])
        for role in (
            "replacementDependencyClosure",
            "sourceStyleFidelity",
            "identityFeatureBinding",
            "multiSubjectClosure",
            "sourceCanvasNormalization",
            "sourceMarkPolicy",
            "generationPromptFrozen",
        )
        if requirement_ids[role] in stage_one_by_id
    ]
    results.extend(
        [
            {
                result_fields["identity"]: requirement_ids[
                    "interactionIntegrity"
                ],
                result_fields["pass"]: interaction_pass,
                result_fields["evidence"]: (
                    f"逐关系肢体与接触拓扑证据 {len(interaction_evidence) if isinstance(interaction_evidence, list) else 0} 项"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "approvedImageQuality"
                ],
                result_fields["pass"]: approved_image_quality_pass,
                result_fields["evidence"]: (
                    "Approved Image 的全部视觉硬门禁与六维画风合同均通过"
                ),
            },
            {
                result_fields["identity"]: requirement_ids["userVisibleCopy"],
                result_fields["pass"]: copy_pass,
                result_fields["evidence"]: (
                    "标题和描述已独立对照 Approved Image，并通过口语自然度与槽位可迁移性复核"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "userVisiblePromptTemplate"
                ],
                result_fields["pass"]: prompt_pass,
                result_fields["evidence"]: (
                    "Prompt Template 逐句职责归类仅包含用户可编辑画面内容"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "highValueSlotBinding"
                ],
                result_fields["pass"]: slot_pass,
                result_fields["evidence"]: (
                    f"高价值槽位 {len(slot_reviews) if isinstance(slot_reviews, list) else 0} 项均绑定可见组件与四道独立门禁"
                ),
            },
            {
                result_fields["identity"]: requirement_ids["subjectEditPolicy"],
                result_fields["pass"]: subject_policy_pass,
                result_fields["evidence"]: (
                    "主体存在性、组件图、Handoff 连续性和 subject 槽策略已独立对账"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "identityInheritancePolicy"
                ],
                result_fields["pass"]: inheritance_pass,
                result_fields["evidence"]: (
                    f"身份继承 {len(inheritance_reviews) if isinstance(inheritance_reviews, list) else 0} 项均通过上传特征、服装和最小模板例外复核"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "conciseSlotDefaults"
                ],
                result_fields["pass"]: default_pass,
                result_fields["evidence"]: (
                    f"默认值 {len(default_reviews) if isinstance(default_reviews, list) else 0} 项均通过用户语言、单轴和最简表述复核"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "resolvedPromptIntegrity"
                ],
                result_fields["pass"]: semantic_check_pass("resolvedPrompts"),
                result_fields["evidence"]: (
                    "默认值与全部推荐值代入后的 resolved prompt 完整、无占位符残留且句式自然"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "openContentAuthority"
                ],
                result_fields["pass"]: bool(
                    semantic_check_pass("openAxes")
                    and gallery_layer.get("pass") is True
                ),
                result_fields["evidence"]: (
                    "主体、文字、颜色、服装、道具和场景的用户字面权限未被隐藏层锁回"
                ),
            },
            {
                result_fields["identity"]: requirement_ids["titlePortability"],
                result_fields["pass"]: semantic_check_pass("maximumDifference"),
                result_fields["evidence"]: (
                    "标题通过每个槽位的最大差异合法输入测试"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "slotSuggestionQuality"
                ],
                result_fields["pass"]: semantic_check_pass("slotSuggestions"),
                result_fields["evidence"]: (
                    "全部推荐值逐项通过同轴、同颗粒度和机制兼容性复核"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "runtimeSemanticsIntegrity"
                ],
                result_fields["pass"]: bool(
                    semantic_check_pass("runtimeSemanticsScope")
                    and semantic_evidence.get("runtimeSemanticsErrors") == []
                ),
                result_fields["evidence"]: (
                    "target、binding、来源隔离、容器依赖与 visualContract 的范围和结构合法"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "runtimeSemanticsResponsibilities"
                ],
                result_fields["pass"]: semantic_check_pass(
                    "runtimeSemanticsResponsibilities"
                ),
                result_fields["evidence"]: (
                    "目标定位、输入绑定和视觉合同的职责已分离"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "identityTextNeutrality"
                ],
                result_fields["pass"]: semantic_check_pass("identityNeutrality"),
                result_fields["evidence"]: (
                    "主体开放时的标题、描述、非主体槽与可见身份文字保持中性"
                ),
            },
            {
                result_fields["identity"]: requirement_ids["visibleTextRouting"],
                result_fields["pass"]: visible_text_pass,
                result_fields["evidence"]: (
                    "全部可见文字区域已独立分类并唯一路由，固定或清理区域无泄漏"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "renderingCoherence"
                ],
                result_fields["pass"]: rendering_coherence_pass,
                result_fields["evidence"]: (
                    "渲染单元完整覆盖组件，subject 完整重绘且身份权限与槽位一致"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "runtimeStyleMedium"
                ],
                result_fields["pass"]: runtime_style_pass,
                result_fields["evidence"]: (
                    "runtimeSemantics 的媒介、画风、构图与关系已直接对照 Approved Image"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "classificationTags"
                ],
                result_fields["pass"]: tag_pass,
                result_fields["evidence"]: (
                    f"内容标签 {len(tag_reviews) if isinstance(tag_reviews, list) else 0} 项均绑定 Approved Image 并具有分类价值"
                ),
            },
            {
                result_fields["identity"]: requirement_ids[
                    "formalRecordContract"
                ],
                result_fields["pass"]: formal_record_pass,
                result_fields["evidence"]: (
                    "正式记录同时通过 Gallery Schema 与正式字段、生产术语和 sidecar 隔离门禁"
                ),
            },
        ]
    )
    expected_ids = set(requirement_ids.values())
    observed_ids = {
        item[result_fields["identity"]]
        for item in results
        if isinstance(item, dict)
        and isinstance(item.get(result_fields["identity"]), str)
    }
    complete = len(results) == len(expected_ids) and observed_ids == expected_ids
    passed = bool(
        complete
        and all(item.get(result_fields["pass"]) is True for item in results)
    )
    return {
        fields["artifactType"]: contract["artifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["requirements"]: results,
        fields["pass"]: passed,
    }
