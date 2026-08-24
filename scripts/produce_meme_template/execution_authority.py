from __future__ import annotations

import re
import weakref
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .adapters import (
    AliyunOssWorkflowAdapters,
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
)
from .workflow_core import (
    LiveAdapterAuthorityError,
    accepted_production_execution_modes,
)


_TRUSTED_LIVE_ADAPTERS: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)
@dataclass(frozen=True)
class _LiveProductionRoles:
    source: Any
    visual_review: Any
    authoring_analysis: Any
    authoring_audit: Any
    semantic_audit: Any
    visual_contract_audit: Any

    def values(self) -> tuple[Any, ...]:
        return (
            self.source,
            self.visual_review,
            self.authoring_analysis,
            self.authoring_audit,
            self.semantic_audit,
            self.visual_contract_audit,
        )


@dataclass(frozen=True, slots=True)
class _IndependentProductionDelegate:
    roles: _LiveProductionRoles
    identities: Mapping[str, str]
    terminal_delegates: tuple[Any, ...]

    def _assert_topology_unchanged(self) -> None:
        current_chains = tuple(
            _adapter_delegate_chain(adapter) for adapter in self.roles.values()
        )
        if (
            len(current_chains) != len(self.terminal_delegates)
            or any(
                chain[-1] is not terminal
                for chain, terminal in zip(
                    current_chains, self.terminal_delegates, strict=True
                )
            )
            or len({id(chain[-1]) for chain in current_chains})
            != len(current_chains)
            or getattr(
                self.roles.source,
                "live_template_identity_method_id",
                None,
            )
            != self.identities["templateIdentityMethodIdentity"]
            or getattr(
                self.roles.visual_review,
                "live_review_method_id",
                None,
            )
            != self.identities["visualReviewMethodIdentity"]
            or getattr(
                self.roles.authoring_analysis,
                "live_authoring_analysis_method_id",
                None,
            )
            != self.identities["authoringAnalysisMethodIdentity"]
            or getattr(
                self.roles.authoring_audit,
                "live_authoring_audit_method_id",
                None,
            )
            != self.identities["authoringAuditMethodIdentity"]
        ):
            raise LiveAdapterAuthorityError(
                "live production adapter topology changed after registration"
            )

    @property
    def live_review_method_id(self) -> str:
        return self.identities["visualReviewMethodIdentity"]

    @property
    def live_template_identity_method_id(self) -> str:
        return self.identities["templateIdentityMethodIdentity"]

    @property
    def live_authoring_analysis_method_id(self) -> str:
        return self.identities["authoringAnalysisMethodIdentity"]

    @property
    def live_authoring_audit_method_id(self) -> str:
        return self.identities["authoringAuditMethodIdentity"]

    def resolve_template_identity(self, source_image: Any, request: Any) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.source.resolve_template_identity(source_image, request)
        self._assert_topology_unchanged()
        return result

    def analyze_source(self, source_image: Any, replacement_strategy: Any) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.source.analyze_source(source_image, replacement_strategy)
        self._assert_topology_unchanged()
        return result

    def inspect_generated(self, generated_image: Any, review_request: Any) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.visual_review.inspect_generated(
            generated_image, review_request
        )
        self._assert_topology_unchanged()
        return result

    def analyze_approved(self, approved_image: Any) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.authoring_analysis.analyze_approved(approved_image)
        self._assert_topology_unchanged()
        return result

    def analyze_approved_with_handoff(
        self, approved_image: Any, authoring_handoff: Any
    ) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.authoring_analysis.analyze_approved_with_handoff(
            approved_image, authoring_handoff
        )
        self._assert_topology_unchanged()
        return result

    def audit_authoring_contract(
        self, approved_image: Any, review_request: Any
    ) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.authoring_audit.audit_authoring_contract(
            approved_image, review_request
        )
        self._assert_topology_unchanged()
        return result

    def audit_semantics(self, content: Any) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.semantic_audit.audit_semantics(content)
        self._assert_topology_unchanged()
        return result

    def audit_visual_contract(
        self, approved_image: Any, review_request: Any
    ) -> Any:
        self._assert_topology_unchanged()
        result = self.roles.visual_contract_audit.audit_visual_contract(
            approved_image, review_request
        )
        self._assert_topology_unchanged()
        return result


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
    roles = _LiveProductionRoles(
        source=source_adapter,
        visual_review=visual_review_adapter,
        authoring_analysis=authoring_analysis_adapter,
        authoring_audit=authoring_audit_adapter,
        semantic_audit=semantic_audit_adapter,
        visual_contract_audit=visual_contract_audit_adapter,
    )
    role_adapters = roles.values()
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
    terminal_delegates = tuple(chain[-1] for chain in delegate_chains)
    delegate = _IndependentProductionDelegate(
        roles,
        MappingProxyType(dict(identities)),
        terminal_delegates,
    )
    fal = FalQueueWorkflowAdapters(delegate, **dict(fal_options or {}))
    live = AliyunOssWorkflowAdapters(fal, **dict(oss_options or {}))
    _TRUSTED_LIVE_ADAPTERS[live] = {
        "identities": dict(identities),
        "roles": roles,
        "terminalDelegates": terminal_delegates,
        "fal": fal,
        "delegate": delegate,
    }
    return live


def _execution_profile(
    *,
    mode: str,
    delivery_eligible: bool,
    topology: str,
    generation_provider: str,
    storage_provider: str,
    identities: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["productionExecutionContract"]
    fields = contract["profileFields"]
    return {
        fields["artifactType"]: contract["artifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["executionMode"]: mode,
        fields["deliveryEligible"]: delivery_eligible,
        fields["adapterTopology"]: topology,
        fields["generationProvider"]: generation_provider,
        fields["storageProvider"]: storage_provider,
        fields["templateIdentityMethodIdentity"]: identities.get(
            "templateIdentityMethodIdentity"
        ),
        fields["visualReviewMethodIdentity"]: identities.get(
            "visualReviewMethodIdentity"
        ),
        fields["authoringAnalysisMethodIdentity"]: identities.get(
            "authoringAnalysisMethodIdentity"
        ),
        fields["authoringAuditMethodIdentity"]: identities.get(
            "authoringAuditMethodIdentity"
        ),
        fields["runtimeInstallSource"]: None,
    }


def resolve_execution_profile(
    adapters: Any,
    execution_mode: Any,
    rules: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve a workflow-owned execution profile before any production side effect."""

    contract = rules["productionExecutionContract"]
    modes = contract["executionModes"]
    mode = modes["recordedReplay"] if execution_mode is None else execution_mode
    accepted_modes = accepted_production_execution_modes(rules)
    if type(mode) is not str or mode not in accepted_modes:
        return None, ["execution mode is missing or unknown"]

    if mode == modes["recordedReplay"]:
        return _execution_profile(
            mode=mode,
            delivery_eligible=False,
            topology=contract["adapterTopologies"]["recordedReplay"],
            generation_provider=contract["recordedProvider"],
            storage_provider=contract["recordedProvider"],
            identities={},
            rules=rules,
        ), []

    oss = adapters if type(adapters) is AliyunOssWorkflowAdapters else None
    fal = oss.delegate if oss is not None else None
    delegate = fal.delegate if type(fal) is FalQueueWorkflowAdapters else None
    if oss is None or type(fal) is not FalQueueWorkflowAdapters or delegate is None:
        return None, ["live production requires Aliyun OSS wrapping Fal"]

    if mode == contract["liveReadinessExecutionMode"]:
        review_method = getattr(delegate, "live_review_method_id", None)
        if review_method not in set(contract["liveReviewMethodIds"]):
            return None, ["live readiness review method is not approved"]
        return _execution_profile(
            mode=mode,
            delivery_eligible=False,
            topology=contract["adapterTopologies"]["liveReadiness"],
            generation_provider=rules["generationExecutionContract"][
                "providerRoles"
            ]["fal"],
            storage_provider=rules["objectStorageContract"]["providerRoles"][
                "aliyunOss"
            ],
            identities={"visualReviewMethodIdentity": review_method},
            rules=rules,
        ), []

    errors: list[str] = []

    registration = _TRUSTED_LIVE_ADAPTERS.get(adapters)
    if (
        type(delegate) is not _IndependentProductionDelegate
        or not isinstance(registration, dict)
    ):
        return None, ["live production adapter topology is not core-registered"]
    registered_roles = registration.get("roles")
    registered_terminals = registration.get("terminalDelegates")
    current_roles = delegate.roles
    current_chains = tuple(
        _adapter_delegate_chain(adapter) for adapter in current_roles.values()
    )
    if (
        adapters.delegate is not registration.get("fal")
        or fal.delegate is not registration.get("delegate")
        or delegate is not registration.get("delegate")
        or current_roles is not registered_roles
        or not isinstance(registered_terminals, tuple)
        or len(current_chains) != len(registered_terminals)
        or any(
            chain[-1] is not terminal
            for chain, terminal in zip(
                current_chains, registered_terminals, strict=True
            )
        )
        or len({id(chain[-1]) for chain in current_chains})
        != len(current_chains)
    ):
        return None, ["live production adapter topology changed after registration"]
    identities: dict[str, Any] = dict(registration.get("identities", {}))
    live_fields = contract["liveAdapterFields"]
    current_identity_values = {
        "templateIdentityMethodIdentity": getattr(
            current_roles.source,
            live_fields["templateIdentityMethodIdentity"],
            None,
        ),
        "visualReviewMethodIdentity": getattr(
            current_roles.visual_review,
            live_fields["visualReviewMethodIdentity"],
            None,
        ),
        "authoringAnalysisMethodIdentity": getattr(
            current_roles.authoring_analysis,
            live_fields["authoringAnalysisMethodIdentity"],
            None,
        ),
        "authoringAuditMethodIdentity": getattr(
            current_roles.authoring_audit,
            live_fields["authoringAuditMethodIdentity"],
            None,
        ),
    }
    if current_identity_values != identities:
        return None, ["live production method identities changed after registration"]
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

    return _execution_profile(
        mode=mode,
        delivery_eligible=True,
        topology=contract["adapterTopologies"]["liveExternal"],
        generation_provider=rules["generationExecutionContract"]["providerRoles"][
            "fal"
        ],
        storage_provider=rules["objectStorageContract"]["providerRoles"][
            "aliyunOss"
        ],
        identities=identities,
        rules=rules,
    ), []


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


def registered_live_adapter_authority_errors(
    adapters: Any,
    rules: dict[str, Any],
) -> list[str]:
    """Revalidate the registered live topology at later delivery boundaries."""

    execution_mode = rules["productionExecutionContract"]["executionModes"][
        "liveExternal"
    ]
    _profile, errors = resolve_execution_profile(adapters, execution_mode, rules)
    return errors


def qualify_runtime_execution_profile(
    profile: dict[str, Any],
    rules: dict[str, Any],
    *,
    production_pin: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Run doctor and bind its authority result through one workflow seam."""

    from .release_management import doctor
    from .workflow_core import REPO_ROOT

    diagnosis = doctor(REPO_ROOT, production_pin=production_pin)
    diagnostics = rules["releaseManagementContract"]["diagnosticFields"]
    if not isinstance(diagnosis, dict):
        return profile, ["INVALID_RUNTIME_DIAGNOSIS"], []
    raw_codes = diagnosis.get(diagnostics["errorCodes"])
    diagnostic_errors = (
        []
        if diagnosis.get("pass") is True
        else (
            list(raw_codes)
            if isinstance(raw_codes, list)
            and all(isinstance(code, str) for code in raw_codes)
            else ["INVALID_RUNTIME_DIAGNOSIS"]
        )
    )
    if diagnostic_errors:
        return profile, diagnostic_errors, []
    bound, execution_errors = bind_runtime_install_source(
        profile,
        diagnosis,
        rules,
    )
    return bound, [], execution_errors


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


def production_execution_profile_errors(
    profile: Any,
    rules: dict[str, Any],
) -> list[str]:
    """Validate the workflow-owned profile for every supported execution mode."""

    contract = rules["productionExecutionContract"]
    fields = contract["profileFields"]
    if not isinstance(profile, dict) or set(profile) != set(fields.values()):
        return ["production execution profile shape is invalid"]
    mode = profile.get(fields["executionMode"])
    if mode not in accepted_production_execution_modes(rules):
        return ["production execution profile mode is invalid"]
    if (
        profile.get(fields["artifactType"]) != contract["artifactType"]
        or profile.get(fields["schemaVersion"]) != rules["schemaVersion"]
    ):
        return ["production execution profile identity is invalid"]
    if mode == contract["executionModes"]["liveExternal"]:
        return delivery_execution_profile_errors(profile, rules)

    recorded = mode == contract["executionModes"]["recordedReplay"]
    expected_topology = contract["adapterTopologies"][
        "recordedReplay" if recorded else "liveReadiness"
    ]
    expected_generation_provider = (
        contract["recordedProvider"]
        if recorded
        else rules["generationExecutionContract"]["providerRoles"]["fal"]
    )
    expected_storage_provider = (
        contract["recordedProvider"]
        if recorded
        else rules["objectStorageContract"]["providerRoles"]["aliyunOss"]
    )
    errors: list[str] = []
    if (
        profile.get(fields["deliveryEligible"]) is not False
        or profile.get(fields["adapterTopology"]) != expected_topology
        or profile.get(fields["generationProvider"])
        != expected_generation_provider
        or profile.get(fields["storageProvider"]) != expected_storage_provider
    ):
        errors.append("production execution profile topology is invalid")
    if recorded:
        identity_roles = (
            "templateIdentityMethodIdentity",
            "visualReviewMethodIdentity",
            "authoringAnalysisMethodIdentity",
            "authoringAuditMethodIdentity",
        )
        if any(profile.get(fields[role]) is not None for role in identity_roles):
            errors.append("recorded replay cannot claim live method identities")
    else:
        if profile.get(fields["visualReviewMethodIdentity"]) not in set(
            contract["liveReviewMethodIds"]
        ):
            errors.append("live readiness visual review method is invalid")
        identity_roles = (
            "templateIdentityMethodIdentity",
            "authoringAnalysisMethodIdentity",
            "authoringAuditMethodIdentity",
        )
        if any(profile.get(fields[role]) is not None for role in identity_roles):
            errors.append("live readiness cannot claim delivery method identities")
    return errors
