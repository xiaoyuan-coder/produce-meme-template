from __future__ import annotations

import re
import weakref
from typing import Any

from .adapters import (
    AliyunOssWorkflowAdapters,
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
)


_TRUSTED_LIVE_ADAPTERS: weakref.WeakKeyDictionary[Any, dict[str, str]] = (
    weakref.WeakKeyDictionary()
)


class _IndependentProductionDelegate:
    def __init__(
        self,
        source_adapter: Any,
        visual_review_adapter: Any,
        authoring_analysis_adapter: Any,
        authoring_audit_adapter: Any,
        semantic_audit_adapter: Any,
        visual_contract_audit_adapter: Any,
        identities: dict[str, str],
    ) -> None:
        self.source_adapter = source_adapter
        self.visual_review_adapter = visual_review_adapter
        self.authoring_analysis_adapter = authoring_analysis_adapter
        self.authoring_audit_adapter = authoring_audit_adapter
        self.semantic_audit_adapter = semantic_audit_adapter
        self.visual_contract_audit_adapter = visual_contract_audit_adapter
        self.live_review_method_id = identities["visualReviewMethodIdentity"]
        self.live_template_identity_method_id = identities[
            "templateIdentityMethodIdentity"
        ]
        self.live_authoring_analysis_method_id = identities[
            "authoringAnalysisMethodIdentity"
        ]
        self.live_authoring_audit_method_id = identities[
            "authoringAuditMethodIdentity"
        ]

    def resolve_template_identity(self, source_image: Any, request: Any) -> Any:
        return self.source_adapter.resolve_template_identity(source_image, request)

    def analyze_source(self, source_image: Any, replacement_strategy: Any) -> Any:
        return self.source_adapter.analyze_source(source_image, replacement_strategy)

    def inspect_generated(self, generated_image: Any, review_request: Any) -> Any:
        return self.visual_review_adapter.inspect_generated(
            generated_image, review_request
        )

    def analyze_approved(self, approved_image: Any) -> Any:
        return self.authoring_analysis_adapter.analyze_approved(approved_image)

    def analyze_approved_with_handoff(
        self, approved_image: Any, authoring_handoff: Any
    ) -> Any:
        return self.authoring_analysis_adapter.analyze_approved_with_handoff(
            approved_image, authoring_handoff
        )

    def audit_authoring_contract(
        self, approved_image: Any, review_request: Any
    ) -> Any:
        return self.authoring_audit_adapter.audit_authoring_contract(
            approved_image, review_request
        )

    def audit_semantics(self, content: Any) -> Any:
        return self.semantic_audit_adapter.audit_semantics(content)

    def audit_visual_contract(
        self, approved_image: Any, review_request: Any
    ) -> Any:
        return self.visual_contract_audit_adapter.audit_visual_contract(
            approved_image, review_request
        )


def _adapter_delegate_chain(adapter: Any) -> tuple[Any, ...]:
    chain: list[Any] = []
    seen: set[int] = set()
    current = adapter
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = getattr(current, "delegate", None)
    return tuple(chain)


def build_live_production_adapters(
    *,
    source_adapter: Any,
    visual_review_adapter: Any,
    authoring_analysis_adapter: Any,
    authoring_audit_adapter: Any,
    semantic_audit_adapter: Any,
    visual_contract_audit_adapter: Any,
    fal_options: dict[str, Any] | None = None,
    oss_options: dict[str, Any] | None = None,
) -> AliyunOssWorkflowAdapters:
    """Build and core-register the only adapter topology eligible for delivery."""

    from .workflow_core import MACHINE_RULES

    contract = MACHINE_RULES["productionExecutionContract"]
    role_adapters = (
        source_adapter,
        visual_review_adapter,
        authoring_analysis_adapter,
        authoring_audit_adapter,
        semantic_audit_adapter,
        visual_contract_audit_adapter,
    )
    delegate_chains = tuple(
        _adapter_delegate_chain(adapter) for adapter in role_adapters
    )
    if (
        len({id(adapter) for adapter in role_adapters}) != len(role_adapters)
        or any(
            isinstance(node, DeterministicFixtureAdapters)
            for chain in delegate_chains
            for node in chain
        )
        or len({id(chain[-1]) for chain in delegate_chains})
        != len(delegate_chains)
    ):
        raise ValueError(
            "live production role adapters must be transitively independent"
        )
    live_fields = contract["liveAdapterFields"]
    identities = {
        role: getattr(adapter, live_fields[role], None)
        for role, adapter in (
            ("templateIdentityMethodIdentity", source_adapter),
            ("visualReviewMethodIdentity", visual_review_adapter),
            ("authoringAnalysisMethodIdentity", authoring_analysis_adapter),
            ("authoringAuditMethodIdentity", authoring_audit_adapter),
        )
    }
    if identities["templateIdentityMethodIdentity"] not in set(
        contract["liveTemplateIdentityMethodIds"]
    ):
        raise ValueError("live template identity method is not approved")
    if identities["visualReviewMethodIdentity"] not in set(
        contract["liveReviewMethodIds"]
    ):
        raise ValueError("live visual review method is not approved")
    if identities["authoringAnalysisMethodIdentity"] == identities[
        "authoringAuditMethodIdentity"
    ]:
        raise ValueError("live authoring analysis and audit method identities must differ")
    if not all(
        type(value) is str
        and re.fullmatch(contract["methodIdentityPattern"], value) is not None
        for value in identities.values()
    ):
        raise ValueError("live production method identities are invalid")
    required_methods = (
        (source_adapter, "resolve_template_identity"),
        (source_adapter, "analyze_source"),
        (visual_review_adapter, "inspect_generated"),
        (authoring_analysis_adapter, "analyze_approved_with_handoff"),
        (authoring_audit_adapter, "audit_authoring_contract"),
        (semantic_audit_adapter, "audit_semantics"),
        (visual_contract_audit_adapter, "audit_visual_contract"),
    )
    if not all(callable(getattr(adapter, method, None)) for adapter, method in required_methods):
        raise ValueError("live production adapter role is missing a required method")
    delegate = _IndependentProductionDelegate(
        source_adapter,
        visual_review_adapter,
        authoring_analysis_adapter,
        authoring_audit_adapter,
        semantic_audit_adapter,
        visual_contract_audit_adapter,
        identities,
    )
    fal = FalQueueWorkflowAdapters(delegate, **dict(fal_options or {}))
    live = AliyunOssWorkflowAdapters(fal, **dict(oss_options or {}))
    _TRUSTED_LIVE_ADAPTERS[live] = identities
    return live


def resolve_execution_profile(
    adapters: Any,
    execution_mode: Any,
    rules: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve a workflow-owned execution profile before any production side effect."""

    contract = rules["productionExecutionContract"]
    modes = contract["executionModes"]
    fields = contract["profileFields"]
    mode = modes["recordedReplay"] if execution_mode is None else execution_mode
    accepted_modes = { *modes.values(), contract["liveReadinessExecutionMode"] }
    if type(mode) is not str or mode not in accepted_modes:
        return None, ["execution mode is missing or unknown"]

    if mode == modes["recordedReplay"]:
        return {
            fields["artifactType"]: contract["artifactType"],
            fields["schemaVersion"]: rules["schemaVersion"],
            fields["executionMode"]: mode,
            fields["deliveryEligible"]: False,
            fields["adapterTopology"]: contract["adapterTopologies"][
                "recordedReplay"
            ],
            fields["generationProvider"]: contract["recordedProvider"],
            fields["storageProvider"]: contract["recordedProvider"],
            fields["templateIdentityMethodIdentity"]: None,
            fields["visualReviewMethodIdentity"]: None,
            fields["authoringAnalysisMethodIdentity"]: None,
            fields["authoringAuditMethodIdentity"]: None,
            fields["runtimeInstallSource"]: None,
        }, []

    oss = adapters if type(adapters) is AliyunOssWorkflowAdapters else None
    fal = oss.delegate if oss is not None else None
    delegate = fal.delegate if type(fal) is FalQueueWorkflowAdapters else None
    if oss is None or type(fal) is not FalQueueWorkflowAdapters or delegate is None:
        return None, ["live production requires Aliyun OSS wrapping Fal"]

    if mode == contract["liveReadinessExecutionMode"]:
        review_method = getattr(delegate, "live_review_method_id", None)
        if review_method not in set(contract["liveReviewMethodIds"]):
            return None, ["live readiness review method is not approved"]
        return {
            fields["artifactType"]: contract["artifactType"],
            fields["schemaVersion"]: rules["schemaVersion"],
            fields["executionMode"]: mode,
            fields["deliveryEligible"]: False,
            fields["adapterTopology"]: contract["adapterTopologies"][
                "liveReadiness"
            ],
            fields["generationProvider"]: rules["generationExecutionContract"][
                "providerRoles"
            ]["fal"],
            fields["storageProvider"]: rules["objectStorageContract"][
                "providerRoles"
            ]["aliyunOss"],
            fields["templateIdentityMethodIdentity"]: None,
            fields["visualReviewMethodIdentity"]: review_method,
            fields["authoringAnalysisMethodIdentity"]: None,
            fields["authoringAuditMethodIdentity"]: None,
            fields["runtimeInstallSource"]: None,
        }, []

    errors: list[str] = []

    registered_identities = _TRUSTED_LIVE_ADAPTERS.get(adapters)
    if (
        type(delegate) is not _IndependentProductionDelegate
        or not isinstance(registered_identities, dict)
    ):
        return None, ["live production adapter topology is not core-registered"]
    identities: dict[str, Any] = dict(registered_identities)
    identity_pattern = contract["methodIdentityPattern"]
    for role, value in identities.items():
        if type(value) is not str or re.fullmatch(identity_pattern, value) is None:
            errors.append(f"{role} is not independently identified")
    if identities.get("visualReviewMethodIdentity") not in set(
        contract["liveReviewMethodIds"]
    ):
        errors.append("visual review method is not approved for live production")
    if identities.get("templateIdentityMethodIdentity") not in set(
        contract["liveTemplateIdentityMethodIds"]
    ):
        errors.append("template identity method is not approved for live production")
    if (
        identities.get("authoringAnalysisMethodIdentity")
        == identities.get("authoringAuditMethodIdentity")
    ):
        errors.append("authoring analysis and audit must be independently routed")
    if errors:
        return None, errors

    return {
        fields["artifactType"]: contract["artifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["executionMode"]: mode,
        fields["deliveryEligible"]: True,
        fields["adapterTopology"]: contract["adapterTopologies"]["liveExternal"],
        fields["generationProvider"]: rules["generationExecutionContract"][
            "providerRoles"
        ]["fal"],
        fields["storageProvider"]: rules["objectStorageContract"][
            "providerRoles"
        ]["aliyunOss"],
        fields["templateIdentityMethodIdentity"]: identities[
            "templateIdentityMethodIdentity"
        ],
        fields["visualReviewMethodIdentity"]: identities[
            "visualReviewMethodIdentity"
        ],
        fields["authoringAnalysisMethodIdentity"]: identities[
            "authoringAnalysisMethodIdentity"
        ],
        fields["authoringAuditMethodIdentity"]: identities[
            "authoringAuditMethodIdentity"
        ],
        fields["runtimeInstallSource"]: None,
    }, []


def bind_runtime_install_source(
    profile: dict[str, Any],
    runtime_diagnosis: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Bind doctor evidence and reject source-worktree live production."""

    contract = rules["productionExecutionContract"]
    fields = contract["profileFields"]
    diagnostics = rules["releaseManagementContract"]["diagnosticFields"]
    install_source = runtime_diagnosis.get(diagnostics["installSource"])
    bound = dict(profile)
    bound[fields["runtimeInstallSource"]] = install_source
    if bound[fields["executionMode"]] == contract["executionModes"]["liveExternal"]:
        errors = delivery_execution_profile_errors(bound, rules)
        if errors:
            return bound, errors
    return bound, []


def delivery_execution_profile_errors(
    profile: Any,
    rules: dict[str, Any],
) -> list[str]:
    """Validate the complete workflow-owned profile required for delivery."""

    contract = rules["productionExecutionContract"]
    fields = contract["profileFields"]
    if not isinstance(profile, dict) or set(profile) != set(fields.values()):
        return ["production execution profile shape is invalid"]
    install_source = profile.get(fields["runtimeInstallSource"])
    analysis_method = profile.get(fields["authoringAnalysisMethodIdentity"])
    audit_method = profile.get(fields["authoringAuditMethodIdentity"])
    errors: list[str] = []
    if profile.get(fields["artifactType"]) != contract["artifactType"]:
        errors.append("production execution profile artifact type is invalid")
    if profile.get(fields["schemaVersion"]) != rules["schemaVersion"]:
        errors.append("production execution profile schema version is invalid")
    if (
        profile.get(fields["executionMode"])
        != contract["executionModes"]["liveExternal"]
        or profile.get(fields["deliveryEligible"]) is not True
        or profile.get(fields["adapterTopology"])
        != contract["adapterTopologies"]["liveExternal"]
    ):
        errors.append("production execution profile is not delivery eligible")
    if (
        profile.get(fields["generationProvider"])
        != rules["generationExecutionContract"]["providerRoles"]["fal"]
        or profile.get(fields["storageProvider"])
        != rules["objectStorageContract"]["providerRoles"]["aliyunOss"]
    ):
        errors.append("production execution providers are not delivery eligible")
    if profile.get(fields["visualReviewMethodIdentity"]) not in set(
        contract["liveReviewMethodIds"]
    ):
        errors.append("production visual review method is not delivery eligible")
    if profile.get(fields["templateIdentityMethodIdentity"]) not in set(
        contract["liveTemplateIdentityMethodIds"]
    ):
        errors.append("production template identity method is not delivery eligible")
    if not all(
        type(value) is str
        and re.fullmatch(contract["methodIdentityPattern"], value) is not None
        for value in (analysis_method, audit_method)
    ) or analysis_method == audit_method:
        errors.append("production authoring methods are not independently identified")
    if (
        not isinstance(install_source, str)
        or not install_source.strip()
        or install_source == contract["sourceWorktreeInstallSource"]
        or install_source.startswith(contract["uninstalledReleasePackagePrefix"])
    ):
        errors.append("live production must run from a verified installed release")
    return errors
