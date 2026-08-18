from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import weakref
from pathlib import Path
from typing import Any

from .adapters import (
    AliyunOssWorkflowAdapters,
    DeterministicFixtureAdapters,
    FalQueueWorkflowAdapters,
)
from .artifacts import (
    load_json_object_or_none as _load_object,
    pretty_json_bytes as _pretty_json_bytes,
    sha256_file as _sha_file,
)
from .template_test import TemplateTestResult, run_template_test
from .release_management import (
    MACHINE_RULES_RELATIVE,
    doctor,
    runtime_production_pin,
)
from .workflow import (
    ProductionResult,
    formal_template_contract_valid,
    run_production,
    validate_production_manifest_lineage,
)


ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = ROOT / "contracts" / "machine-rules.json"
_CONSTRUCTED_LIVE_ADAPTERS: weakref.WeakSet[Any] = weakref.WeakSet()
_LIVE_ADAPTER_CONTEXTS: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)


def _rules() -> dict[str, Any]:
    value = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("machine rules must contain an object")
    return value


def _json_bytes(value: Any) -> bytes:
    return _pretty_json_bytes(value, sort_keys=True)


def _sha_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _empty_release_gates(contract: dict[str, Any]) -> dict[str, Any]:
    fields = contract["releaseGateFields"]
    return {
        fields["historicalRegressionPass"]: False,
        fields["fullValidationPass"]: False,
        fields["releasePackageDigest"]: None,
        fields["runtimePinSha256"]: None,
        fields["freshInstallPass"]: False,
        fields["doctorPass"]: False,
        fields["installedForwardPass"]: False,
        fields["standardsReviewClean"]: False,
        fields["specReviewClean"]: False,
    }


def _release_gates_valid(value: Any, contract: dict[str, Any]) -> bool:
    fields = contract["releaseGateFields"]
    return bool(
        isinstance(value, dict)
        and set(value) == set(fields.values())
        and all(
            value.get(fields[role]) is True
            for role in (
                "historicalRegressionPass",
                "fullValidationPass",
                "freshInstallPass",
                "doctorPass",
                "installedForwardPass",
                "standardsReviewClean",
                "specReviewClean",
            )
        )
        and isinstance(value.get(fields["releasePackageDigest"]), str)
        and len(value[fields["releasePackageDigest"]]) == 64
        and isinstance(value.get(fields["runtimePinSha256"]), str)
        and len(value[fields["runtimePinSha256"]]) == 64
        and all(
            character in "0123456789abcdef"
            for digest_role in ("releasePackageDigest", "runtimePinSha256")
            for character in value[fields[digest_role]]
        )
    )


def _ordinary_json_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    try:
        if not path.is_absolute() or ".." in path.parts:
            return None
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                return None
        if not path.is_file():
            return None
        return path.resolve()
    except OSError:
        return None


def _ordinary_directory_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    try:
        if not path.is_absolute() or ".." in path.parts:
            return None
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink():
                return None
        return path.resolve() if path.is_dir() else None
    except OSError:
        return None


def verify_code_review_receipt(
    value: Any,
    *,
    expected_sha256: Any,
    expected_axis: str,
    expected_reviewed_git_commit: str,
    expected_pin_sha256: str,
    rules: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
) -> bool:
    rules = rules or _rules()
    contract = contract or rules["releaseReadinessContract"]
    path = _ordinary_json_path(str(value) if isinstance(value, Path) else value)
    receipt = _load_object(path) if path is not None else None
    fields = contract["reviewReceiptFields"]
    return bool(
        receipt is not None
        and isinstance(expected_sha256, str)
        and len(expected_sha256) == 64
        and path is not None
        and _sha_file(path) == expected_sha256
        and set(receipt) == set(fields.values())
        and receipt.get(fields["artifactType"])
        == contract["reviewReceiptArtifactType"]
        and receipt.get(fields["schemaVersion"]) == rules["schemaVersion"]
        and receipt.get(fields["axis"]) == expected_axis
        and isinstance(receipt.get(fields["comparisonBaseGitCommit"]), str)
        and re.fullmatch(
            r"[0-9a-f]{40}",
            receipt[fields["comparisonBaseGitCommit"]],
        )
        and receipt.get(fields["reviewedGitCommit"])
        == expected_reviewed_git_commit
        and isinstance(expected_reviewed_git_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", expected_reviewed_git_commit)
        and receipt.get(fields["runtimePinSha256"])
        == expected_pin_sha256
        and receipt.get(fields["clean"]) is True
        and receipt.get(fields["findingCount"]) == 0
    )


def _verified_release_gates(
    evidence: Any,
    *,
    corpus_items: dict[str, dict[str, Any]],
    rules: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any] | None:
    evidence_fields = contract["releaseGateEvidenceFields"]
    gate_fields = contract["releaseGateFields"]
    if not (
        isinstance(evidence, dict)
        and set(evidence) == set(evidence_fields.values())
    ):
        return None
    release_contract = rules["releaseManagementContract"]
    package_value = evidence.get(evidence_fields["releasePackagePath"])
    installed_value = evidence.get(evidence_fields["installedRuntimePath"])
    package = (
        _ordinary_json_path(
            str(Path(package_value) / release_contract["lockFileName"])
        )
        if isinstance(package_value, str)
        else None
    )
    installed_rules = (
        _ordinary_json_path(
            str(Path(installed_value) / MACHINE_RULES_RELATIVE)
        )
        if isinstance(installed_value, str)
        else None
    )
    if package is None or installed_rules is None:
        return None
    package_root = package.parent
    installed_root = installed_rules.parents[1]
    expected_digest = evidence.get(evidence_fields["expectedReleaseDigest"])
    if not (
        isinstance(expected_digest, str)
        and len(expected_digest) == 64
        and all(character in "0123456789abcdef" for character in expected_digest)
    ):
        return None
    try:
        package_diagnostic = doctor(package_root)
        installed_diagnostic = doctor(installed_root)
        installed_pin = runtime_production_pin(installed_root)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    diagnostic_fields = release_contract["diagnosticFields"]
    pin_fields = release_contract["productionPinFields"]
    if not (
        package_diagnostic.get("pass") is True
        and installed_diagnostic.get("pass") is True
        and package_diagnostic.get(diagnostic_fields["releaseLockSha256"])
        == expected_digest
        and installed_diagnostic.get(diagnostic_fields["releaseLockSha256"])
        == expected_digest
        and installed_diagnostic.get(diagnostic_fields["installSource"])
        == str(package_root)
    ):
        return None
    installed_pin_sha = _sha_json(installed_pin)
    git_commit = installed_pin.get(pin_fields["gitCommit"])
    if not isinstance(git_commit, str):
        return None
    forward_output = _ordinary_directory_path(
        evidence.get(evidence_fields["installedForwardOutputPath"])
    )
    if forward_output is None:
        return None
    try:
        manifest_path = _production_manifest_path(forward_output, contract)
        pin_path = _lineage_artifact_path(
            forward_output, "productionPin", contract
        )
        safe_manifest_path = _ordinary_json_path(str(manifest_path))
        safe_pin_path = _ordinary_json_path(str(pin_path))
        manifest = (
            _load_object(safe_manifest_path)
            if safe_manifest_path is not None
            else None
        )
        if (
            manifest is None
            or safe_manifest_path is None
            or safe_pin_path is None
            or not isinstance(
                evidence.get(
                    evidence_fields["installedForwardManifestSha256"]
                ),
                str,
            )
            or _sha_file(safe_manifest_path)
            != evidence[evidence_fields["installedForwardManifestSha256"]]
            or _load_object(safe_pin_path) != installed_pin
            or validate_production_manifest_lineage(forward_output, manifest)
            or manifest.get("outcome") != "completed"
            or manifest.get("state") != "FINALIZED"
        ):
            return None
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    corpus_fields = contract["corpusScenarioFields"]
    forward_corpus = corpus_items.get(contract["forwardScenarioRole"])
    if not (
        isinstance(forward_corpus, dict)
        and manifest.get("sourceImageSha256")
        == forward_corpus.get(corpus_fields["sourceSha256"])
    ):
        return None
    receipts_valid = all(
        verify_code_review_receipt(
            evidence.get(evidence_fields[f"{axis_role}ReviewReceiptPath"]),
            expected_sha256=evidence.get(
                evidence_fields[f"{axis_role}ReviewReceiptSha256"]
            ),
            expected_axis=contract["reviewAxes"][axis_role],
            expected_reviewed_git_commit=git_commit,
            expected_pin_sha256=installed_pin_sha,
            rules=rules,
            contract=contract,
        )
        for axis_role in ("standards", "spec")
    )
    if not receipts_valid:
        return None
    return {
        gate_fields["historicalRegressionPass"]: True,
        gate_fields["fullValidationPass"]: True,
        gate_fields["releasePackageDigest"]: expected_digest,
        gate_fields["runtimePinSha256"]: installed_pin_sha,
        gate_fields["freshInstallPass"]: True,
        gate_fields["doctorPass"]: True,
        gate_fields["installedForwardPass"]: True,
        gate_fields["standardsReviewClean"]: True,
        gate_fields["specReviewClean"]: True,
    }


def _request_ledger(
    request: dict[str, Any],
    contract: dict[str, Any],
    rules: dict[str, Any],
    corpus_sha256: str,
) -> dict[str, Any]:
    fields = contract["requestLedgerFields"]
    return {
        fields["artifactType"]: contract["requestArtifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["requestSha256"]: _sha_json(request),
        fields["corpusSha256"]: corpus_sha256,
        fields["request"]: copy.deepcopy(request),
    }


def _lineage_artifact_path(
    output_directory: Path,
    role: str,
    contract: dict[str, Any],
) -> Path:
    return output_directory / contract["requiredLineageArtifacts"][role]


def _production_manifest_path(
    output_directory: Path, contract: dict[str, Any]
) -> Path:
    return output_directory / contract["productionManifestFileName"]


def _lineage_sha_by_role(
    output_dir: Path,
    contract: dict[str, Any],
) -> dict[str, str] | None:
    manifest_path = _production_manifest_path(output_dir, contract)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or not isinstance(artifacts, dict):
        return None
    result: dict[str, str] = {}
    for role, relative in contract["requiredLineageArtifacts"].items():
        matching = (
            [name for name in artifacts if name.startswith(relative + ".")]
            if role in contract["prefixLineageArtifactRoles"]
            else [relative] if relative in artifacts else []
        )
        if len(matching) != 1:
            return None
        artifact_name = matching[0]
        record = artifacts.get(artifact_name)
        artifact_path = output_dir / artifact_name
        if not (
            isinstance(record, dict)
            and isinstance(record.get("sha256"), str)
            and artifact_path.is_file()
            and not artifact_path.is_symlink()
            and artifact_path.resolve().is_relative_to(output_dir.resolve())
            and _sha_file(artifact_path) == record["sha256"]
        ):
            return None
        result[role] = record["sha256"]
    return result


def _corpus_by_role(
    contract: dict[str, Any],
) -> dict[str, dict[str, Any]] | None:
    corpus = _load_object(ROOT / contract["corpusRelativePath"])
    fields = contract["corpusFields"]
    scenario_fields = contract["corpusScenarioFields"]
    scenarios = corpus.get(fields["scenarios"]) if corpus is not None else None
    expected_corpus_fields = set(fields.values())
    expected_scenario_fields = set(scenario_fields.values())
    if not (
        corpus is not None
        and set(corpus) == expected_corpus_fields
        and corpus.get(fields["artifactType"]) == contract["corpusArtifactType"]
        and corpus.get(fields["executionMode"])
        == contract["executionModes"]["recordedReplay"]
        and corpus.get(fields["generator"]) == contract["recordedCorpusGenerator"]
        and isinstance(scenarios, list)
        and all(isinstance(item, dict) for item in scenarios)
        and all(set(item) == expected_scenario_fields for item in scenarios)
    ):
        return None
    roles = [item.get(scenario_fields["role"]) for item in scenarios]
    if not (
        all(isinstance(role, str) for role in roles)
        and len(roles) == len(set(roles))
    ):
        return None
    if set(roles) != {
        *contract["scenarioRoles"].values(),
        contract["forwardScenarioRole"],
    }:
        return None
    corpus_root = (ROOT / contract["corpusRelativePath"]).parent.resolve()
    for item in scenarios:
        role = item[scenario_fields["role"]]
        role_key = _scenario_role_key(role, contract)
        if role_key is None:
            return None
        fixture_directory = contract["fixtureDirectoryByScenarioRoleKey"].get(
            role_key
        )
        if not isinstance(fixture_directory, str):
            return None
        expected_paths = {
            "sourcePath": f"{fixture_directory}/source-image.jpg",
            "approvedPath": f"{fixture_directory}/approved-template-image.png",
        }
        for path_role, sha_role in (
            ("sourcePath", "sourceSha256"),
            ("approvedPath", "approvedSha256"),
        ):
            relative = item.get(scenario_fields[path_role])
            digest = item.get(scenario_fields[sha_role])
            if not (
                isinstance(relative, str)
                and relative
                and relative == expected_paths[path_role]
                and ".." not in Path(relative).parts
                and isinstance(digest, str)
                and len(digest) == 64
            ):
                return None
            path = (corpus_root / relative).resolve()
            if not (
                path.is_file()
                and not path.is_symlink()
                and path.is_relative_to(corpus_root)
                and _sha_file(path) == digest
            ):
                return None
        if not all(
            isinstance(item.get(scenario_fields[role]), str)
            and item[scenario_fields[role]].strip()
            for role in (
                "expectedSourceCategoryRole",
                "sourcePageUrl",
                "sourceLicense",
                "replacementValue",
            )
        ):
            return None
    return {item[scenario_fields["role"]]: item for item in scenarios}


def _scenario_role_key(
    role: Any, contract: dict[str, Any]
) -> str | None:
    for role_key, value in contract["scenarioRoles"].items():
        if value == role:
            return role_key
    return "unseenForward" if role == contract["forwardScenarioRole"] else None


def _fixture_directory(role: Any, contract: dict[str, Any]) -> str | None:
    role_key = _scenario_role_key(role, contract)
    return (
        contract["fixtureDirectoryByScenarioRoleKey"].get(role_key)
        if role_key is not None
        else None
    )


class RecordedShadowReadinessAdapters:
    """Replay the reviewed real-image shadow corpus through production seams."""

    def __init__(self, corpus_root: str | Path | None = None) -> None:
        rules = _rules()
        default_root = (ROOT / rules["releaseReadinessContract"]["corpusRelativePath"]).parent
        self.corpus_root = Path(corpus_root or default_root).resolve()
        self.scenario_adapters: dict[str, DeterministicFixtureAdapters] = {}
        self.execution_mode = rules["releaseReadinessContract"]["executionModes"][
            "recordedReplay"
        ]

    def workflow_adapters_for_scenario(
        self, scenario: dict[str, Any]
    ) -> DeterministicFixtureAdapters:
        contract = _rules()["releaseReadinessContract"]
        role = scenario.get(contract["scenarioFields"]["role"])
        if role not in {
            *contract["scenarioRoles"].values(),
            contract["forwardScenarioRole"],
        }:
            raise ValueError("unknown shadow scenario role")
        cached = self.scenario_adapters.get(role)
        if cached is not None:
            return cached
        fixture_directory = _fixture_directory(role, contract)
        if fixture_directory is None:
            raise ValueError("shadow scenario fixture role is unbound")
        fixture_dir = (self.corpus_root / fixture_directory).resolve()
        if not (
            fixture_dir.is_dir()
            and not fixture_dir.is_symlink()
            and fixture_dir.is_relative_to(self.corpus_root)
        ):
            raise ValueError("shadow scenario fixture is unsafe")
        adapters = DeterministicFixtureAdapters(fixture_dir)
        adapters.approved_image_path_override = (
            fixture_dir / "approved-template-image.png"
        )
        self.scenario_adapters[role] = adapters
        return adapters

    def template_test_adapters_for_scenario(
        self, scenario: dict[str, Any]
    ) -> DeterministicFixtureAdapters:
        return self.workflow_adapters_for_scenario(scenario)


class _LiveReviewWorkflowDelegate:
    """Keep source analysis deterministic while reviewing every new live image."""

    def __init__(self, source_delegate: Any, review_delegate: Any) -> None:
        self.source_delegate = source_delegate
        self.review_delegate = review_delegate

    @property
    def generate_calls(self) -> list[dict[str, Any]]:
        return self.source_delegate.generate_calls

    @property
    def upload_calls(self) -> list[dict[str, Any]]:
        return self.source_delegate.upload_calls

    def analyze_source(
        self, source_image: Path, replacement_strategy: dict[str, Any] | None
    ) -> dict[str, Any]:
        return self.source_delegate.analyze_source(
            source_image, replacement_strategy
        )

    def inspect_generated(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.review_delegate.inspect_generated(
            generated_image, review_request
        )

    def analyze_approved(self, approved_image: Path) -> dict[str, Any]:
        return self.review_delegate.analyze_approved(approved_image)

    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]:
        return self.review_delegate.audit_semantics(content)

    def inspect_template_test(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]:
        return self.review_delegate.inspect_template_test(
            generated_image, review_request
        )


class LiveShadowReadinessAdapters(RecordedShadowReadinessAdapters):
    """Use Fal and Aliyun with explicit reviewers for each newly generated image."""

    def __init__(
        self,
        corpus_root: str | Path | None = None,
        *,
        live_review_adapters_by_role: dict[str, Any] | None = None,
    ) -> None:
        preflight = live_release_readiness_preflight()
        rules = _rules()
        contract = rules["releaseReadinessContract"]
        fields = contract["externalExecutionFields"]
        statuses = contract["externalExecutionStatuses"]
        if preflight[fields["status"]] != statuses["ready"]:
            raise RuntimeError("live release readiness credentials are incomplete")
        expected_roles = {
            *contract["scenarioRoles"].values(),
            contract["forwardScenarioRole"],
        }
        if not (
            isinstance(live_review_adapters_by_role, dict)
            and set(live_review_adapters_by_role) == expected_roles
            and all(
                not isinstance(adapter, DeterministicFixtureAdapters)
                and getattr(
                    adapter,
                    contract["liveReviewAdapterFields"]["methodIdentity"],
                    None,
                )
                in set(contract["liveReviewMethodIds"])
                for adapter in live_review_adapters_by_role.values()
            )
        ):
            raise RuntimeError(
                "live release readiness requires an independent reviewer for every role"
            )
        super().__init__(corpus_root)
        self.execution_mode = contract["executionModes"]["liveExternal"]
        _LIVE_ADAPTER_CONTEXTS[self] = {
            "corpusRoot": self.corpus_root,
            "reviewers": dict(live_review_adapters_by_role),
            "workflows": {},
        }
        _CONSTRUCTED_LIVE_ADAPTERS.add(self)

    def workflow_adapters_for_scenario(self, scenario: dict[str, Any]) -> Any:
        contract = _rules()["releaseReadinessContract"]
        role = scenario.get(contract["scenarioFields"]["role"])
        context = _LIVE_ADAPTER_CONTEXTS.get(self)
        if not isinstance(context, dict):
            raise RuntimeError("live adapter capability is not registered")
        workflows = context["workflows"]
        cached = workflows.get(role)
        if cached is not None:
            return cached
        fixture_directory = _fixture_directory(role, contract)
        corpus_root = context["corpusRoot"]
        if fixture_directory is None or not isinstance(corpus_root, Path):
            raise ValueError("live scenario fixture role is unbound")
        fixture_dir = (corpus_root / fixture_directory).resolve()
        if not (
            fixture_dir.is_dir()
            and not fixture_dir.is_symlink()
            and fixture_dir.is_relative_to(corpus_root)
        ):
            raise ValueError("live scenario fixture is unsafe")
        recorded = DeterministicFixtureAdapters(fixture_dir)
        reviewer = context["reviewers"].get(role)
        if reviewer is None:
            raise ValueError("live scenario reviewer is missing")
        fal = FalQueueWorkflowAdapters(
            _LiveReviewWorkflowDelegate(recorded, reviewer)
        )
        live = AliyunOssWorkflowAdapters(
            fal,
            public_base_url=os.environ[
                contract["liveCredentialEnvironment"]["ossPublicBaseUrl"]
            ],
        )
        workflows[role] = live
        return live


def _core_constructed_live_adapter(value: Any) -> bool:
    try:
        return bool(
            type(value) is LiveShadowReadinessAdapters
            and value in _CONSTRUCTED_LIVE_ADAPTERS
            and isinstance(_LIVE_ADAPTER_CONTEXTS.get(value), dict)
        )
    except (AttributeError, TypeError):
        return False


def _core_live_workflow_adapter(
    owner: LiveShadowReadinessAdapters,
    scenario: dict[str, Any],
    contract: dict[str, Any],
) -> AliyunOssWorkflowAdapters:
    role = scenario.get(contract["scenarioFields"]["role"])
    adapter = LiveShadowReadinessAdapters.workflow_adapters_for_scenario(
        owner, copy.deepcopy(scenario)
    )
    context = _LIVE_ADAPTER_CONTEXTS.get(owner)
    fal = adapter.delegate if type(adapter) is AliyunOssWorkflowAdapters else None
    live_delegate = fal.delegate if type(fal) is FalQueueWorkflowAdapters else None
    if not (
        isinstance(context, dict)
        and context["workflows"].get(role) is adapter
        and type(live_delegate) is _LiveReviewWorkflowDelegate
        and type(live_delegate.source_delegate) is DeterministicFixtureAdapters
        and live_delegate.review_delegate is context["reviewers"].get(role)
    ):
        raise RuntimeError("live adapter topology is not core-owned")
    return adapter


def recorded_shadow_request() -> dict[str, Any]:
    """Build the canonical six-role request from the tracked shadow corpus."""

    rules = _rules()
    contract = rules["releaseReadinessContract"]
    scenario_fields = contract["scenarioFields"]
    corpus_fields = contract["corpusScenarioFields"]
    corpus_items = _corpus_by_role(contract)
    if corpus_items is None:
        raise ValueError("shadow corpus is invalid")
    corpus_root = (ROOT / contract["corpusRelativePath"]).parent
    scenarios: list[dict[str, Any]] = []
    for role in contract["scenarioRoles"].values():
        item = corpus_items.get(role)
        if item is None:
            raise ValueError("shadow corpus coverage is incomplete")
        fixture_directory = _fixture_directory(role, contract)
        if fixture_directory is None:
            raise ValueError("shadow scenario fixture role is unbound")
        fixture_dir = corpus_root / fixture_directory
        request = _load_object(fixture_dir / "request.json")
        if request is None:
            raise ValueError("shadow production request is invalid")
        request["sourceImage"] = str(
            (corpus_root / item[corpus_fields["sourcePath"]]).resolve()
        )
        scenarios.append(
            {
                scenario_fields["role"]: role,
                scenario_fields["productionRequest"]: request,
                scenario_fields["executionMode"]: contract["executionModes"][
                    "recordedReplay"
                ],
                scenario_fields["sourceProvenance"]: {
                    "sourcePageUrl": item[corpus_fields["sourcePageUrl"]],
                    "sourceSha256": item[corpus_fields["sourceSha256"]],
                },
            }
        )
    forward_role = contract["forwardScenarioRole"]
    forward_item = corpus_items.get(forward_role)
    if forward_item is None:
        raise ValueError("unseen forward corpus item is missing")
    forward_directory = _fixture_directory(forward_role, contract)
    if forward_directory is None:
        raise ValueError("unseen forward fixture role is unbound")
    forward_fixture = corpus_root / forward_directory
    forward_request = _load_object(forward_fixture / "request.json")
    if forward_request is None:
        raise ValueError("unseen forward production request is invalid")
    forward_request["sourceImage"] = str(
        (corpus_root / forward_item[corpus_fields["sourcePath"]]).resolve()
    )
    forward_scenario = {
        scenario_fields["role"]: forward_role,
        scenario_fields["productionRequest"]: forward_request,
        scenario_fields["executionMode"]: contract["executionModes"][
            "recordedReplay"
        ],
        scenario_fields["sourceProvenance"]: {
            "sourcePageUrl": forward_item[corpus_fields["sourcePageUrl"]],
            "sourceSha256": forward_item[corpus_fields["sourceSha256"]],
        },
    }
    return {
        contract["requestFields"]["scenarios"]: scenarios,
        contract["requestFields"]["forwardScenario"]: forward_scenario,
        contract["requestFields"]["releaseGateEvidence"]: None,
    }


def live_shadow_request(
    release_gate_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rules = _rules()
    contract = rules["releaseReadinessContract"]
    scenario_fields = contract["scenarioFields"]
    request_fields = contract["requestFields"]
    request = recorded_shadow_request()
    for scenario in [
        *request[request_fields["scenarios"]],
        request[request_fields["forwardScenario"]],
    ]:
        scenario[scenario_fields["executionMode"]] = contract["executionModes"][
            "liveExternal"
        ]
    request[request_fields["releaseGateEvidence"]] = copy.deepcopy(
        release_gate_evidence
    )
    return request


def _request_scenario_valid(
    scenario: Any,
    *,
    expected_role: str,
    contract: dict[str, Any],
    adapter_mode: Any,
) -> bool:
    fields = contract["scenarioFields"]
    provenance_fields = contract["sourceProvenanceFields"]
    production_fields = contract["productionRequestFields"]
    if not isinstance(scenario, dict) or set(scenario) != set(fields.values()):
        return False
    production = scenario.get(fields["productionRequest"])
    provenance = scenario.get(fields["sourceProvenance"])
    return bool(
        isinstance(scenario.get(fields["role"]), str)
        and scenario.get(fields["role"]) == expected_role
        and scenario.get(fields["executionMode"]) == adapter_mode
        and isinstance(production, dict)
        and all(
            isinstance(production.get(production_fields[role]), str)
            and production[production_fields[role]].strip()
            for role in ("templateKey", "sourceImage", "productionItemIdentity")
        )
        and isinstance(provenance, dict)
        and set(provenance) == set(provenance_fields.values())
        and isinstance(provenance.get(provenance_fields["sourcePageUrl"]), str)
        and provenance[provenance_fields["sourcePageUrl"]].startswith("https://")
        and isinstance(provenance.get(provenance_fields["sourceSha256"]), str)
        and len(provenance[provenance_fields["sourceSha256"]]) == 64
    )


def _request_scenario_matches_corpus(
    scenario: dict[str, Any],
    corpus_item: dict[str, Any],
    contract: dict[str, Any],
) -> bool:
    scenario_fields = contract["scenarioFields"]
    provenance_fields = contract["sourceProvenanceFields"]
    corpus_fields = contract["corpusScenarioFields"]
    role = scenario[scenario_fields["role"]]
    fixture_directory = _fixture_directory(role, contract)
    if fixture_directory is None:
        return False
    corpus_root = (ROOT / contract["corpusRelativePath"]).parent
    expected_request = _load_object(
        corpus_root / fixture_directory / "request.json"
    )
    provenance = scenario[scenario_fields["sourceProvenance"]]
    if expected_request is None or not isinstance(provenance, dict):
        return False
    source_path = (
        corpus_root / corpus_item[corpus_fields["sourcePath"]]
    ).resolve()
    expected_request["sourceImage"] = str(source_path)
    production_request = scenario[scenario_fields["productionRequest"]]
    return bool(
        production_request == expected_request
        and provenance.get(provenance_fields["sourcePageUrl"])
        == corpus_item.get(corpus_fields["sourcePageUrl"])
        and provenance.get(provenance_fields["sourceSha256"])
        == corpus_item.get(corpus_fields["sourceSha256"])
    )


def live_release_readiness_preflight(
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    rules = _rules()
    contract = rules["releaseReadinessContract"]
    fields = contract["externalExecutionFields"]
    statuses = contract["externalExecutionStatuses"]
    values = os.environ if environ is None else environ
    missing = [
        role
        for role, variable in contract["liveCredentialEnvironment"].items()
        if not isinstance(values.get(variable), str) or not values[variable].strip()
    ]
    return {
        fields["status"]: (
            statuses["notRunMissingCredentials"] if missing else statuses["ready"]
        ),
        fields["missingCredentialRoles"]: missing,
    }


def _scenario_evidence_error(
    *,
    scenario: dict[str, Any],
    result: ProductionResult,
    lineage: dict[str, str],
    corpus_item: dict[str, Any] | None,
    rules: dict[str, Any],
    contract: dict[str, Any],
) -> str | None:
    errors = contract["errorCodes"]
    scenario_fields = contract["scenarioFields"]
    corpus_fields = contract["corpusScenarioFields"]
    role = scenario[scenario_fields["role"]]
    role_key = next(
        (
            key
            for key, value in contract["scenarioRoles"].items()
            if value == role
        ),
        None,
    )
    category_key = (
        contract["scenarioSourceCategoryRoleKeys"].get(role_key)
        if role_key is not None
        else contract["forwardSourceCategoryRoleKey"]
        if role == contract["forwardScenarioRole"]
        else None
    )
    if corpus_item is None or category_key is None:
        return errors["provenanceMismatch"]
    source_path_value = corpus_item.get(corpus_fields["sourcePath"])
    approved_sha = corpus_item.get(corpus_fields["approvedSha256"])
    source_sha = corpus_item.get(corpus_fields["sourceSha256"])
    provenance = scenario.get(scenario_fields["sourceProvenance"])
    production_request = scenario.get(scenario_fields["productionRequest"])
    provenance_fields = contract["sourceProvenanceFields"]
    production_fields = contract["productionRequestFields"]
    execution_mode = scenario.get(scenario_fields["executionMode"])
    try:
        corpus_root = (ROOT / contract["corpusRelativePath"]).parent
        corpus_source = (corpus_root / source_path_value).resolve()
        request_source = Path(
            production_request[production_fields["sourceImage"]]
        ).resolve()
    except (KeyError, TypeError, ValueError, OSError):
        return errors["provenanceMismatch"]
    if not (
        isinstance(source_path_value, str)
        and corpus_source.is_file()
        and not corpus_source.is_symlink()
        and corpus_source.is_relative_to(ROOT)
        and request_source == corpus_source
        and isinstance(source_sha, str)
        and _sha_file(corpus_source) == source_sha
        and isinstance(provenance, dict)
        and provenance.get(provenance_fields["sourceSha256"]) == source_sha
        and provenance.get(provenance_fields["sourcePageUrl"])
        == corpus_item.get(corpus_fields["sourcePageUrl"])
        and lineage.get("sourceImage") == source_sha
        and isinstance(approved_sha, str)
        and (
            execution_mode == contract["executionModes"]["liveExternal"]
            or lineage.get("approvedTemplateImage") == approved_sha
        )
        and corpus_item.get(corpus_fields["expectedSourceCategoryRole"])
        == category_key
    ):
        return errors["provenanceMismatch"]
    source_analysis = _load_object(
        _lineage_artifact_path(result.output_dir, "sourceAnalysis", contract)
    )
    replacement_plan = _load_object(
        _lineage_artifact_path(result.output_dir, "replacementPlan", contract)
    )
    expected_category = rules["sourceCategories"][category_key]
    try:
        source_category = source_analysis["target"]["category"]
        plan_category = replacement_plan["primaryTargets"][0]["sourceCategory"]
        replacement_value = replacement_plan["primaryTargets"][0][
            "replacementValue"
        ]
    except (KeyError, IndexError, TypeError):
        return errors["scenarioRoleMismatch"]
    if (
        source_category != expected_category
        or plan_category != expected_category
        or replacement_value
        != corpus_item.get(corpus_fields["replacementValue"])
    ):
        return errors["scenarioRoleMismatch"]
    formal = _load_object(
        _lineage_artifact_path(result.output_dir, "formalTemplate", contract)
    )
    final_validation = _load_object(
        _lineage_artifact_path(
            result.output_dir, "finalValidationReport", contract
        )
    )
    top_level = rules["formalProjection"]["topLevel"]
    if not (
        formal is not None
        and set(formal) == set(top_level.values())
        and formal.get(top_level["coverAsset"])
        == formal.get(top_level["referenceAsset"])
        and formal_template_contract_valid(formal, rules)
        and final_validation is not None
        and final_validation.get("pass") is True
        and final_validation.get("coverMatchesReferenceImage") is True
        and final_validation.get("topLevelExtra") == []
        and final_validation.get("topLevelMissing") == []
    ):
        return errors["formalProjectionMismatch"]
    return None


def _live_execution_evidence_valid(
    output_dir: Path,
    lineage: dict[str, str],
    rules: dict[str, Any],
) -> bool:
    generation = rules["generationExecutionContract"]
    storage = rules["objectStorageContract"]
    readiness = rules["releaseReadinessContract"]
    wal_fields = generation["walFields"]
    receipt_fields = storage["receiptFields"]
    wal = _load_object(_lineage_artifact_path(output_dir, "generationWal", readiness))
    receipt = _load_object(
        _lineage_artifact_path(output_dir, "assetReceipt", readiness)
    )
    visual_review = _load_object(
        _lineage_artifact_path(output_dir, "visualReview", readiness)
    )
    candidate_sha = lineage.get("generatedCandidate")
    approved_sha = lineage.get("approvedTemplateImage")
    if wal is None or receipt is None or visual_review is None:
        return False
    live_evidence_fields = readiness["liveReviewEvidenceFields"]
    method = visual_review.get(live_evidence_fields["method"])
    return bool(
        wal.get(wal_fields["status"]) == generation["walStatuses"]["succeeded"]
        and wal.get(wal_fields["provider"])
        == generation["providerRoles"]["fal"]
        and isinstance(wal.get(wal_fields["providerRequestIdentity"]), str)
        and wal[wal_fields["providerRequestIdentity"]].strip()
        and isinstance(wal.get(wal_fields["providerOutputIdentity"]), str)
        and wal[wal_fields["providerOutputIdentity"]].strip()
        and candidate_sha == approved_sha
        and wal.get(wal_fields["outputSha256"]) == candidate_sha
        and receipt.get(receipt_fields["provider"])
        == storage["providerRoles"]["aliyunOss"]
        and receipt.get(receipt_fields["approvedImageSha256"]) == approved_sha
        and isinstance(
            receipt.get(receipt_fields["providerRequestIdentity"]), str
        )
        and receipt[receipt_fields["providerRequestIdentity"]].strip()
        and receipt.get(receipt_fields["uploadStatus"])
        in set(storage["uploadStatuses"].values())
        and isinstance(method, dict)
        and method.get(live_evidence_fields["methodIdentity"])
        in set(readiness["liveReviewMethodIds"])
    )


def _template_test_report_valid(
    report_path: Path,
    expected_request: dict[str, Any],
    expected_template_sha256: str,
    rules: dict[str, Any],
) -> bool:
    contract = rules["templateTestContract"]
    report_fields = contract["reportFields"]
    case_fields = contract["caseFields"]
    case_report_fields = contract["caseReportFields"]
    generation_fields = contract["generationRequestFields"]
    request_fields = contract["requestFields"]
    report = _load_object(report_path)
    expected_cases = expected_request.get(request_fields["cases"])
    cases = report.get(report_fields["cases"]) if report is not None else None
    if not (
        report is not None
        and report.get(report_fields["outcome"]) == "completed"
        and report.get(report_fields["templateJsonSha256"])
        == expected_template_sha256
        and isinstance(expected_cases, list)
        and isinstance(cases, list)
        and len(cases) == len(expected_cases)
        and all(isinstance(item, dict) for item in cases)
    ):
        return False
    by_id = {
        item.get(case_report_fields["caseIdentity"]): item for item in cases
    }
    if len(by_id) != len(cases):
        return False
    for expected in expected_cases:
        if not isinstance(expected, dict):
            return False
        case_id = expected.get(case_fields["caseIdentity"])
        actual = by_id.get(case_id)
        mode = expected.get(case_fields["mode"])
        if not isinstance(actual, dict):
            return False
        expected_input = (
            {case_fields["slotValues"]: expected.get(case_fields["slotValues"])}
            if mode == contract["modes"]["slotEdit"]
            else {case_fields["freePrompt"]: expected.get(case_fields["freePrompt"])}
        )
        generation_request = actual.get(
            case_report_fields["generationRequest"]
        )
        if not (
            actual.get(case_report_fields["mode"]) == mode
            and actual.get(case_report_fields["userInput"]) == expected_input
            and actual.get(case_report_fields["outcome"]) == "completed"
            and actual.get(case_report_fields["reviewPass"]) is True
            and actual.get(case_report_fields["visibleDeviations"]) == []
            and isinstance(generation_request, dict)
            and generation_request.get(generation_fields["prompt"])
            == actual.get(case_report_fields["resolvedPrompt"])
        ):
            return False
    return True


def _template_test_values(template: dict[str, Any]) -> dict[str, str] | None:
    values: dict[str, str] = {}
    input_schema = template.get("inputSchema")
    if not isinstance(input_schema, list) or not input_schema:
        return None
    for item in input_schema:
        if not isinstance(item, dict):
            return None
        slot_id = item.get("id")
        slot_type = item.get("type")
        value: Any = None
        if slot_type == "subject":
            text = item.get("text")
            suggestions = text.get("suggestions") if isinstance(text, dict) else None
            value = suggestions[0] if isinstance(suggestions, list) and suggestions else None
            if value is None and isinstance(text, dict):
                value = text.get("defaultValue")
        elif slot_type == "prompt":
            suggestions = item.get("suggestions")
            value = suggestions[0] if isinstance(suggestions, list) and suggestions else None
        elif slot_type == "select":
            options = item.get("options")
            first = options[0] if isinstance(options, list) and options else None
            value = first.get("value") if isinstance(first, dict) else None
        if not (
            isinstance(slot_id, str)
            and isinstance(value, str)
            and value.strip()
        ):
            return None
        values[slot_id] = value.strip()
    return values


def _template_test_request(
    *,
    role: str,
    production_output: Path,
    template: dict[str, Any],
    rules: dict[str, Any],
    readiness_contract: dict[str, Any],
) -> dict[str, Any] | None:
    values = _template_test_values(template)
    manifest = _load_object(
        _production_manifest_path(production_output, readiness_contract)
    )
    revision = manifest.get("revision") if manifest is not None else None
    if values is None or not isinstance(revision, int) or isinstance(revision, bool):
        return None
    contract = rules["templateTestContract"]
    request_fields = contract["requestFields"]
    case_fields = contract["caseFields"]
    config = readiness_contract["templateTest"]
    return {
        request_fields["templateJsonPath"]: str(
            _lineage_artifact_path(
                production_output, "formalTemplate", readiness_contract
            )
        ),
        request_fields["templateRevision"]: revision,
        request_fields["invocationIdentity"]: f"release-readiness-{role.replace('_', '-')}",
        request_fields["cases"]: [
            {
                case_fields["caseIdentity"]: config["slotCaseIdentity"],
                case_fields["mode"]: contract["modes"]["slotEdit"],
                case_fields["slotValues"]: values,
            },
            {
                case_fields["caseIdentity"]: config["freeCaseIdentity"],
                case_fields["mode"]: contract["modes"]["freeEdit"],
                case_fields["freePrompt"]: (
                    f"{config['freePromptPrefix']}：{role}；使用全新字面输入，"
                    "保持参考图的构图、层级与视觉关系。"
                ),
            },
        ],
    }


def _sampled_template_test_roles(
    roles: set[str], contract: dict[str, Any]
) -> list[str]:
    config = contract["templateTest"]
    return sorted(
        roles,
        key=lambda role: hashlib.sha256(
            f"{config['selectionSalt']}:{role}".encode("utf-8")
        ).hexdigest(),
    )[: config["sampleCount"]]


def _safe_readiness_root(output_root: str | Path) -> Path | None:
    if type(output_root) not in (str, type(Path())):
        return None
    try:
        lexical = Path(output_root).absolute()
        if ".." in Path(output_root).parts:
            return None
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            candidate = current / part
            if candidate.is_symlink():
                if current == Path(lexical.anchor):
                    current = candidate.resolve()
                    continue
                return None
            current = candidate
        resolved = current.resolve()
        if resolved.exists() and not resolved.is_dir():
            return None
        if (
            resolved == ROOT
            or resolved.is_relative_to(ROOT)
            or ROOT.is_relative_to(resolved)
        ):
            return None
        return resolved
    except (TypeError, ValueError, OSError, RuntimeError):
        return None


def _safe_workspace(root: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative or ".." in Path(relative).parts:
        return None
    path = root / relative
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return None
    resolved = path.resolve()
    if resolved.parent != root or resolved == root:
        return None
    return resolved


def _publish_report(path: Path, report: dict[str, Any]) -> bool:
    payload = _json_bytes(report)
    if path.exists():
        return path.is_file() and not path.is_symlink() and path.read_bytes() == payload
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return path.is_file() and not path.is_symlink() and path.read_bytes() == payload
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _optional_sha256(value: Any) -> bool:
    return value is None or bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _terminal_report_shape_valid(
    report: dict[str, Any],
    normalized_request: dict[str, Any],
    sampled_roles: list[str],
    *,
    rules: dict[str, Any],
    contract: dict[str, Any],
) -> bool:
    report_fields = contract["reportFields"]
    scenario_fields = contract["scenarioFields"]
    scenario_report_fields = contract["scenarioReportFields"]
    production_fields = contract["productionRequestFields"]
    request_fields = contract["requestFields"]
    scenarios = report.get(report_fields["scenarios"])
    forward = report.get(report_fields["forwardScenario"])
    expected_scenarios = normalized_request[request_fields["scenarios"]]
    expected_forward = normalized_request[request_fields["forwardScenario"]]
    if not (
        type(report.get(report_fields["pass"])) is bool
        and report.get(report_fields["outcome"])
        == contract["outcomes"][
            "passed" if report[report_fields["pass"]] else "failed"
        ]
        and (
            report.get(report_fields["errorCode"]) is None
            if report[report_fields["pass"]]
            else report.get(report_fields["errorCode"])
            in set(contract["errorCodes"].values())
        )
        and type(report.get(report_fields["releaseEligible"])) is bool
        and (report[report_fields["pass"]] or not report[report_fields["releaseEligible"]])
        and isinstance(scenarios, list)
        and len(scenarios) == len(expected_scenarios)
        and all(isinstance(item, dict) for item in scenarios)
        and isinstance(forward, dict)
        and report.get(report_fields["templateTestScenarioRoles"]) == sampled_roles
    ):
        return False
    external_fields = contract["externalExecutionFields"]
    external = report.get(report_fields["externalExecution"])
    gates = report.get(report_fields["releaseGates"])
    gate_fields = contract["releaseGateFields"]
    if not (
        isinstance(external, dict)
        and set(external) == set(external_fields.values())
        and external.get(external_fields["status"])
        in set(contract["externalExecutionStatuses"].values())
        and isinstance(external.get(external_fields["missingCredentialRoles"]), list)
        and all(
            isinstance(role, str)
            for role in external[external_fields["missingCredentialRoles"]]
        )
        and len(external[external_fields["missingCredentialRoles"]])
        == len(set(external[external_fields["missingCredentialRoles"]]))
        and set(external[external_fields["missingCredentialRoles"]])
        <= set(contract["liveCredentialEnvironment"])
        and isinstance(gates, dict)
        and set(gates) == set(gate_fields.values())
        and all(
            type(gates.get(gate_fields[role])) is bool
            for role in (
                "historicalRegressionPass",
                "fullValidationPass",
                "freshInstallPass",
                "doctorPass",
                "installedForwardPass",
                "standardsReviewClean",
                "specReviewClean",
            )
        )
        and _optional_sha256(gates.get(gate_fields["releasePackageDigest"]))
        and _optional_sha256(gates.get(gate_fields["runtimePinSha256"]))
    ):
        return False
    for item, expected in zip(
        [*scenarios, forward],
        [*expected_scenarios, expected_forward],
        strict=True,
    ):
        item_pass = item.get(scenario_report_fields["pass"])
        lineage = item.get(scenario_report_fields["lineageSha256ByRole"])
        if not (
            set(item) == set(scenario_report_fields.values())
            and item.get(scenario_report_fields["role"])
            == expected[scenario_fields["role"]]
            and item.get(scenario_report_fields["productionItemId"])
            == expected[scenario_fields["productionRequest"]][
                production_fields["productionItemIdentity"]
            ]
            and item.get(scenario_report_fields["executionMode"])
            == expected[scenario_fields["executionMode"]]
            and type(item_pass) is bool
            and item.get(scenario_report_fields["outcome"])
            == contract["outcomes"]["passed" if item_pass else "failed"]
            and (
                item.get(scenario_report_fields["productionOutcome"]) is None
                or item.get(scenario_report_fields["productionOutcome"])
                in set(rules["resultStates"])
            )
            and (
                item.get(scenario_report_fields["errorCode"]) is None
                if item_pass
                else item.get(scenario_report_fields["errorCode"])
                in set(contract["errorCodes"].values())
            )
            and (
                item.get(scenario_report_fields["outputDirectory"]) is None
                or isinstance(
                    item.get(scenario_report_fields["outputDirectory"]), str
                )
            )
            and (
                lineage == {}
                or (
                    isinstance(lineage, dict)
                    and set(lineage) == set(contract["requiredLineageArtifacts"])
                    and all(_optional_sha256(value) and value is not None for value in lineage.values())
                )
            )
            and _optional_sha256(
                item.get(scenario_report_fields["formalTemplateSha256"])
            )
            and (
                item.get(scenario_report_fields["templateTestOutcome"]) is None
                or isinstance(
                    item.get(scenario_report_fields["templateTestOutcome"]), str
                )
            )
            and (
                item.get(scenario_report_fields["templateTestOutputDirectory"])
                is None
                or isinstance(
                    item.get(
                        scenario_report_fields["templateTestOutputDirectory"]
                    ),
                    str,
                )
            )
            and _optional_sha256(
                item.get(scenario_report_fields["templateTestReportSha256"])
            )
        ):
            return False
    return True


def _successful_report_artifacts_valid(
    report: dict[str, Any],
    normalized_request: dict[str, Any],
    readiness_root: Path,
    sampled_roles: list[str],
    *,
    rules: dict[str, Any],
    contract: dict[str, Any],
) -> bool:
    report_fields = contract["reportFields"]
    scenario_fields = contract["scenarioFields"]
    scenario_report_fields = contract["scenarioReportFields"]
    production_fields = contract["productionRequestFields"]
    request_fields = contract["requestFields"]
    scenarios = report.get(report_fields["scenarios"])
    forward = report.get(report_fields["forwardScenario"])
    expected_scenarios = normalized_request[request_fields["scenarios"]]
    expected_forward = normalized_request[request_fields["forwardScenario"]]
    expected_all = [*expected_scenarios, expected_forward]
    all_live = all(
        item.get(scenario_fields["executionMode"])
        == contract["executionModes"]["liveExternal"]
        for item in expected_all
    )
    gate_evidence = normalized_request[request_fields["releaseGateEvidence"]]
    corpus_items = _corpus_by_role(contract)
    verified_gates = (
        _verified_release_gates(
            gate_evidence,
            corpus_items=corpus_items,
            rules=rules,
            contract=contract,
        )
        if all_live and gate_evidence is not None and corpus_items is not None
        else None
    )
    expected_gates = (
        verified_gates
        if verified_gates is not None
        else _empty_release_gates(contract)
    )
    external_fields = contract["externalExecutionFields"]
    external_statuses = contract["externalExecutionStatuses"]
    external = report.get(report_fields["externalExecution"])
    recorded_external_valid = bool(
        isinstance(external, dict)
        and set(external) == set(external_fields.values())
        and external.get(external_fields["status"])
        == external_statuses["recordedReplayOnly"]
        and external.get(external_fields["missingCredentialRoles"]) == []
    )
    live_external_valid = bool(
        isinstance(external, dict)
        and set(external) == set(external_fields.values())
        and external.get(external_fields["status"])
        == external_statuses["completed"]
        and external.get(external_fields["missingCredentialRoles"]) == []
    )
    expected_eligible = bool(all_live and verified_gates is not None)
    if not (
        report.get(report_fields["pass"]) is True
        and report.get(report_fields["outcome"]) == contract["outcomes"]["passed"]
        and report.get(report_fields["errorCode"]) is None
        and isinstance(scenarios, list)
        and len(scenarios) == len(expected_scenarios)
        and all(isinstance(item, dict) for item in scenarios)
        and isinstance(forward, dict)
        and report.get(report_fields["templateTestScenarioRoles"]) == sampled_roles
        and report.get(report_fields["releaseGates"]) == expected_gates
        and report.get(report_fields["releaseEligible"]) is expected_eligible
        and (live_external_valid if all_live else recorded_external_valid)
    ):
        return False
    reports = [*scenarios, forward]
    expected = [*expected_scenarios, expected_forward]
    for item, expected_scenario in zip(reports, expected, strict=True):
        role = expected_scenario[scenario_fields["role"]]
        production_request = expected_scenario[scenario_fields["productionRequest"]]
        output_directory = _ordinary_directory_path(
            item.get(scenario_report_fields["outputDirectory"])
        )
        if not (
            set(item) == set(scenario_report_fields.values())
            and item.get(scenario_report_fields["role"]) == role
            and item.get(scenario_report_fields["productionItemId"])
            == production_request[production_fields["productionItemIdentity"]]
            and item.get(scenario_report_fields["executionMode"])
            == expected_scenario[scenario_fields["executionMode"]]
            and item.get(scenario_report_fields["pass"]) is True
            and item.get(scenario_report_fields["outcome"])
            == contract["outcomes"]["passed"]
            and item.get(scenario_report_fields["productionOutcome"]) == "completed"
            and item.get(scenario_report_fields["errorCode"]) is None
            and output_directory is not None
            and output_directory.is_relative_to(readiness_root)
        ):
            return False
        manifest_path = _ordinary_json_path(
            str(_production_manifest_path(output_directory, contract))
        )
        formal_path = _ordinary_json_path(
            str(_lineage_artifact_path(output_directory, "formalTemplate", contract))
        )
        manifest = _load_object(manifest_path) if manifest_path is not None else None
        formal = _load_object(formal_path) if formal_path is not None else None
        lineage = _lineage_sha_by_role(output_directory, contract)
        if not (
            manifest is not None
            and formal is not None
            and not validate_production_manifest_lineage(output_directory, manifest)
            and manifest.get("state") == rules["resultStates"]["completed"]
            and manifest.get("outcome") == "completed"
            and lineage
            == item.get(scenario_report_fields["lineageSha256ByRole"])
            and _sha_file(formal_path)
            == item.get(scenario_report_fields["formalTemplateSha256"])
        ):
            return False
        if (
            expected_scenario[scenario_fields["executionMode"]]
            == contract["executionModes"]["liveExternal"]
            and not _live_execution_evidence_valid(output_directory, lineage, rules)
        ):
            return False
        if verified_gates is not None and lineage.get("productionPin") != verified_gates[
            contract["releaseGateFields"]["runtimePinSha256"]
        ]:
            return False
        test_outcome = item.get(scenario_report_fields["templateTestOutcome"])
        test_directory_value = item.get(
            scenario_report_fields["templateTestOutputDirectory"]
        )
        test_sha = item.get(scenario_report_fields["templateTestReportSha256"])
        if role not in sampled_roles:
            if test_outcome is not None or test_directory_value is not None or test_sha is not None:
                return False
            continue
        test_directory = _ordinary_directory_path(test_directory_value)
        if test_directory is None or not test_directory.is_relative_to(readiness_root):
            return False
        test_report_path = _ordinary_json_path(
            str(
                test_directory
                / rules["templateTestContract"]["artifactNames"]["report"]
            )
        )
        expected_test_request = _template_test_request(
            role=role,
            production_output=output_directory,
            template=formal,
            rules=rules,
            readiness_contract=contract,
        )
        if not (
            test_outcome == "completed"
            and test_report_path is not None
            and isinstance(test_sha, str)
            and _sha_file(test_report_path) == test_sha
            and expected_test_request is not None
            and _template_test_report_valid(
                test_report_path,
                expected_test_request,
                _sha_file(formal_path),
                rules,
            )
        ):
            return False
    return True


def _report_completion(
    report: dict[str, Any],
    request_ledger: dict[str, Any],
    *,
    rules: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    fields = contract["completionFields"]
    ledger_fields = contract["requestLedgerFields"]
    return {
        fields["artifactType"]: contract["completionArtifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["requestSha256"]: request_ledger[ledger_fields["requestSha256"]],
        fields["corpusSha256"]: request_ledger[ledger_fields["corpusSha256"]],
        fields["reportSha256"]: _sha_json(report),
    }


def _existing_terminal_report(
    report_path: Path,
    completion_path: Path,
    ledger_path: Path,
    expected_ledger: dict[str, Any],
    normalized_request: dict[str, Any],
    readiness_root: Path,
    sampled_roles: list[str],
    *,
    rules: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    report_present = report_path.exists() or report_path.is_symlink()
    completion_present = completion_path.exists() or completion_path.is_symlink()
    if not report_present and not completion_present:
        return "absent", None
    report_fields = contract["reportFields"]
    ledger_fields = contract["requestLedgerFields"]
    completion_fields = contract["completionFields"]
    try:
        common_valid = bool(
            ledger_path.is_file()
            and not ledger_path.is_symlink()
            and ledger_path.read_bytes() == _json_bytes(expected_ledger)
        )
        if not common_valid or not report_present:
            return "conflict", None
        report = _load_object(report_path)
        report_valid = bool(
            report_path.is_file()
            and not report_path.is_symlink()
            and report is not None
            and set(report) == set(report_fields.values())
            and report.get(report_fields["artifactType"])
            == contract["artifactType"]
            and report.get(report_fields["schemaVersion"])
            == rules["schemaVersion"]
            and report.get(report_fields["requestSha256"])
            == expected_ledger[ledger_fields["requestSha256"]]
            and report.get(report_fields["corpusSha256"])
            == expected_ledger[ledger_fields["corpusSha256"]]
            and report.get(report_fields["reportPath"]) == str(report_path)
        )
        if not report_valid:
            return "conflict", None
        if not _terminal_report_shape_valid(
            report,
            normalized_request,
            sampled_roles,
            rules=rules,
            contract=contract,
        ):
            return "conflict", None
        if (
            report.get(report_fields["pass"]) is True
            and not _successful_report_artifacts_valid(
                report,
                normalized_request,
                readiness_root,
                sampled_roles,
                rules=rules,
                contract=contract,
            )
        ):
            return "conflict", None
        expected_completion = _report_completion(
            report, expected_ledger, rules=rules, contract=contract
        )
        if completion_present:
            completion = _load_object(completion_path)
            if not (
                completion_path.is_file()
                and not completion_path.is_symlink()
                and completion is not None
                and set(completion) == set(completion_fields.values())
                and completion == expected_completion
            ):
                return "conflict", None
        elif not _publish_report(completion_path, expected_completion):
            return "conflict", None
        return "complete", report
    except OSError:
        return "conflict", None


def _failed_readiness_report(
    error_code: str,
    *,
    rules: dict[str, Any],
    contract: dict[str, Any],
    request_sha256: str | None = None,
    corpus_sha256: str | None = None,
    scenarios: list[Any] | None = None,
    forward_scenario: Any = None,
) -> dict[str, Any]:
    fields = contract["reportFields"]
    return {
        fields["artifactType"]: contract["artifactType"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["requestSha256"]: request_sha256,
        fields["corpusSha256"]: corpus_sha256,
        fields["pass"]: False,
        fields["outcome"]: contract["outcomes"]["failed"],
        fields["errorCode"]: error_code,
        fields["scenarios"]: copy.deepcopy(scenarios or []),
        fields["forwardScenario"]: copy.deepcopy(forward_scenario),
        fields["templateTestScenarioRoles"]: [],
        fields["externalExecution"]: live_release_readiness_preflight(),
        fields["releaseGates"]: _empty_release_gates(contract),
        fields["releaseEligible"]: False,
        fields["reportPath"]: None,
    }


def verify_release_readiness_completion(
    output_root: str | Path,
    *,
    expected_package_path: str | Path,
    expected_release_digest: str,
    expected_git_commit: str,
) -> dict[str, Any]:
    """Replay a completed live readiness workspace for stable promotion."""

    rules = _rules()
    contract = rules["releaseReadinessContract"]
    errors = contract["errorCodes"]
    failure = {
        "pass": False,
        "errorCode": errors["releaseGateIncomplete"],
    }
    try:
        root = _safe_readiness_root(output_root)
        package = Path(expected_package_path).resolve()
    except (TypeError, ValueError, OSError, RuntimeError):
        return failure
    if root is None:
        return {
            **failure,
            "message": "readiness workspace path is unsafe",
        }
    ledger_path = root / contract["requestFileName"]
    report_path = root / contract["reportFileName"]
    completion_path = root / contract["completionFileName"]
    if any(
        not path.is_file() or path.is_symlink()
        for path in (ledger_path, report_path, completion_path)
    ):
        return failure
    ledger = _load_object(ledger_path)
    ledger_fields = contract["requestLedgerFields"]
    if not (
        ledger is not None
        and set(ledger) == set(ledger_fields.values())
        and ledger.get(ledger_fields["artifactType"])
        == contract["requestArtifactType"]
        and ledger.get(ledger_fields["schemaVersion"]) == rules["schemaVersion"]
        and isinstance(ledger.get(ledger_fields["request"]), dict)
    ):
        return failure
    request = ledger[ledger_fields["request"]]
    request_fields = contract["requestFields"]
    scenario_fields = contract["scenarioFields"]
    production_fields = contract["productionRequestFields"]
    if set(request) != set(request_fields.values()):
        return failure
    scenarios = request.get(request_fields["scenarios"])
    forward = request.get(request_fields["forwardScenario"])
    required_roles = list(contract["scenarioRoles"].values())
    if not isinstance(scenarios, list) or not isinstance(forward, dict):
        return failure
    by_role = {
        item.get(scenario_fields["role"]): item
        for item in scenarios
        if isinstance(item, dict)
    }
    if set(by_role) != set(required_roles):
        return failure
    ordered = [by_role[role] for role in required_roles]
    all_scenarios = [*ordered, forward]
    corpus_items = _corpus_by_role(contract)
    if not (
        corpus_items is not None
        and forward.get(scenario_fields["role"]) == contract["forwardScenarioRole"]
        and all(
            _request_scenario_valid(
                item,
                expected_role=role,
                contract=contract,
                adapter_mode=contract["executionModes"]["liveExternal"],
            )
            and _request_scenario_matches_corpus(item, corpus_items[role], contract)
            for role, item in [
                *zip(required_roles, ordered, strict=True),
                (contract["forwardScenarioRole"], forward),
            ]
        )
        and len(
            {
                item[scenario_fields["productionRequest"]][
                    production_fields["productionItemIdentity"]
                ]
                for item in all_scenarios
            }
        )
        == len(all_scenarios)
        and len(
            {
                item[scenario_fields["productionRequest"]][
                    production_fields["templateKey"]
                ]
                for item in all_scenarios
            }
        )
        == len(all_scenarios)
    ):
        return failure
    normalized_request = {
        request_fields["scenarios"]: copy.deepcopy(ordered),
        request_fields["forwardScenario"]: copy.deepcopy(forward),
        request_fields["releaseGateEvidence"]: copy.deepcopy(
            request.get(request_fields["releaseGateEvidence"])
        ),
    }
    corpus_sha = _sha_file(ROOT / contract["corpusRelativePath"])
    expected_ledger = _request_ledger(
        normalized_request, contract, rules, corpus_sha
    )
    sampled_roles = _sampled_template_test_roles(set(required_roles), contract)
    status, report = _existing_terminal_report(
        report_path,
        completion_path,
        ledger_path,
        expected_ledger,
        normalized_request,
        root,
        sampled_roles,
        rules=rules,
        contract=contract,
    )
    report_fields = contract["reportFields"]
    evidence_fields = contract["releaseGateEvidenceFields"]
    gate_fields = contract["releaseGateFields"]
    evidence = normalized_request[request_fields["releaseGateEvidence"]]
    release_contract = rules["releaseManagementContract"]
    lock = _load_object(package / release_contract["lockFileName"])
    lock_fields = release_contract["lockFields"]
    if not (
        status == "complete"
        and report is not None
        and report.get(report_fields["pass"]) is True
        and report.get(report_fields["releaseEligible"]) is True
        and isinstance(evidence, dict)
        and Path(evidence[evidence_fields["releasePackagePath"]]).resolve()
        == package
        and evidence.get(evidence_fields["expectedReleaseDigest"])
        == expected_release_digest
        and report.get(report_fields["releaseGates"], {}).get(
            gate_fields["releasePackageDigest"]
        )
        == expected_release_digest
        and lock is not None
        and lock.get(lock_fields["releaseDigest"]) == expected_release_digest
        and lock.get(lock_fields["gitCommit"]) == expected_git_commit
    ):
        return failure
    return {
        "pass": True,
        "errorCode": None,
        "reportPath": str(report_path),
        "completionPath": str(completion_path),
        "releaseDigest": expected_release_digest,
        "gitCommit": expected_git_commit,
    }


def run_release_readiness(
    request: Any,
    output_root: str | Path,
    adapters: Any,
) -> dict[str, Any]:
    """Run the pre-1.0 shadow and forward-readiness gate.

    The first vertical slice rejects an incomplete representative corpus before
    creating a release-readiness workspace or invoking an external adapter.
    """
    rules = _rules()
    contract = rules["releaseReadinessContract"]
    request_fields = contract["requestFields"]
    scenario_fields = contract["scenarioFields"]
    report_fields = contract["reportFields"]
    outcomes = contract["outcomes"]
    errors = contract["errorCodes"]
    scenarios = (
        request.get(request_fields["scenarios"])
        if isinstance(request, dict)
        else None
    )
    forward_scenario = (
        request.get(request_fields["forwardScenario"])
        if isinstance(request, dict)
        else None
    )
    release_gate_evidence = (
        request.get(request_fields["releaseGateEvidence"])
        if isinstance(request, dict)
        else None
    )
    observed_roles = [
        scenario.get(scenario_fields["role"])
        for scenario in scenarios or []
        if isinstance(scenario, dict)
    ]
    required_roles = set(contract["scenarioRoles"].values())
    valid_list = isinstance(scenarios, list)
    roles_are_strings = bool(
        valid_list
        and len(observed_roles) == len(scenarios)
        and all(isinstance(role, str) and role for role in observed_roles)
    )
    try:
        adapter_mode = getattr(adapters, "execution_mode", None)
    except (AttributeError, TypeError, ValueError, KeyError):
        adapter_mode = None
    mode_role_by_value = {
        value: role for role, value in contract["executionModes"].items()
    }
    adapter_mode_role = (
        mode_role_by_value.get(adapter_mode)
        if type(adapter_mode) is str
        else None
    )
    expected_request_fields = set(request_fields.values())
    complete = (
        isinstance(request, dict)
        and set(request) == expected_request_fields
        and valid_list
        and roles_are_strings
        and len(observed_roles) == len(set(observed_roles))
        and set(observed_roles) == required_roles
        and isinstance(forward_scenario, dict)
        and forward_scenario.get(scenario_fields["role"])
        == contract["forwardScenarioRole"]
    )
    error_code = None
    verified_release_gates: dict[str, Any] | None = None
    if not isinstance(request, dict) or set(request) != expected_request_fields:
        error_code = errors["invalidRequest"]
    elif not valid_list or not roles_are_strings:
        error_code = errors["invalidRequest"]
    elif not complete:
        error_code = errors["coverageMissing"]
    elif adapter_mode_role is None:
        error_code = errors["invalidRequest"]
    elif (
        adapter_mode == contract["executionModes"]["liveExternal"]
        and not _core_constructed_live_adapter(adapters)
    ):
        error_code = errors["liveEvidenceMismatch"]
    else:
        scenario_by_role = {
            scenario[scenario_fields["role"]]: scenario for scenario in scenarios
        }
        ordered_scenarios = [
            scenario_by_role[role]
            for role in contract["scenarioRoles"].values()
        ]
        all_scenarios = [*ordered_scenarios, forward_scenario]
        corpus_items = _corpus_by_role(contract)
        request_shape_valid = all(
            _request_scenario_valid(
                scenario,
                expected_role=role,
                contract=contract,
                adapter_mode=adapter_mode,
            )
            for role, scenario in [
                *[
                    (role, scenario_by_role[role])
                    for role in contract["scenarioRoles"].values()
                ],
                (contract["forwardScenarioRole"], forward_scenario),
            ]
        )
        production_fields = contract["productionRequestFields"]
        production_ids = (
            [
                scenario[scenario_fields["productionRequest"]][
                    production_fields["productionItemIdentity"]
                ]
                for scenario in all_scenarios
            ]
            if request_shape_valid
            else []
        )
        template_keys = (
            [
                scenario[scenario_fields["productionRequest"]][
                    production_fields["templateKey"]
                ]
                for scenario in all_scenarios
            ]
            if request_shape_valid
            else []
        )
        if not (
            request_shape_valid
            and corpus_items is not None
            and all(
                _request_scenario_matches_corpus(
                    scenario,
                    corpus_items[scenario[scenario_fields["role"]]],
                    contract,
                )
                for scenario in all_scenarios
            )
            and len(production_ids) == len(set(production_ids))
            and len(template_keys) == len(set(template_keys))
        ):
            error_code = errors["invalidRequest"]
        elif release_gate_evidence is not None:
            if adapter_mode != contract["executionModes"]["liveExternal"]:
                error_code = errors["invalidRequest"]
            else:
                verified_release_gates = _verified_release_gates(
                    release_gate_evidence,
                    corpus_items=corpus_items,
                    rules=rules,
                    contract=contract,
                )
                if verified_release_gates is None:
                    error_code = errors["releaseGateIncomplete"]
    if error_code is not None:
        return _failed_readiness_report(
            error_code,
            rules=rules,
            contract=contract,
            scenarios=scenarios if valid_list else [],
            forward_scenario=forward_scenario,
        )

    readiness_root = _safe_readiness_root(output_root)
    if readiness_root is None:
        return _failed_readiness_report(
            errors["invalidRequest"], rules=rules, contract=contract
        )
    workspaces = contract["workspaceDirectories"]
    production_base = _safe_workspace(readiness_root, workspaces["production"])
    template_test_base = _safe_workspace(
        readiness_root, workspaces["templateTests"]
    )
    if production_base is None or template_test_base is None:
        return _failed_readiness_report(
            errors["invalidRequest"], rules=rules, contract=contract
        )
    normalized_request = {
        request_fields["scenarios"]: copy.deepcopy(ordered_scenarios),
        request_fields["forwardScenario"]: copy.deepcopy(forward_scenario),
        request_fields["releaseGateEvidence"]: copy.deepcopy(
            release_gate_evidence
        ),
    }
    mode_directory = contract["executionModes"][adapter_mode_role].replace(
        "_", "-"
    )
    production_root = _safe_workspace(production_base, mode_directory)
    template_test_root = _safe_workspace(template_test_base, mode_directory)
    if production_root is None or template_test_root is None:
        return _failed_readiness_report(
            errors["invalidRequest"], rules=rules, contract=contract
        )
    corpus_sha256 = _sha_file(ROOT / contract["corpusRelativePath"])
    sampled_roles = _sampled_template_test_roles(required_roles, contract)
    request_ledger = _request_ledger(
        normalized_request, contract, rules, corpus_sha256
    )
    ledger_path = readiness_root / contract["requestFileName"]
    report_path = readiness_root / contract["reportFileName"]
    completion_path = readiness_root / contract["completionFileName"]
    terminal_status, terminal_report = _existing_terminal_report(
        report_path,
        completion_path,
        ledger_path,
        request_ledger,
        normalized_request,
        readiness_root,
        sampled_roles,
        rules=rules,
        contract=contract,
    )
    if terminal_status == "complete" and terminal_report is not None:
        return terminal_report
    if terminal_status == "conflict":
        ledger_fields = contract["requestLedgerFields"]
        return _failed_readiness_report(
            errors["reportConflict"],
            rules=rules,
            contract=contract,
            request_sha256=request_ledger[ledger_fields["requestSha256"]],
            corpus_sha256=request_ledger[ledger_fields["corpusSha256"]],
        )
    if not _publish_report(ledger_path, request_ledger):
        ledger_fields = contract["requestLedgerFields"]
        return _failed_readiness_report(
            errors["reportConflict"],
            rules=rules,
            contract=contract,
            request_sha256=request_ledger[ledger_fields["requestSha256"]],
            corpus_sha256=request_ledger[ledger_fields["corpusSha256"]],
        )
    scenario_report_fields = contract["scenarioReportFields"]
    scenario_reports: list[dict[str, Any]] = []
    forward_report: dict[str, Any] | None = None
    overall_error: str | None = None
    for scenario in [*ordered_scenarios, forward_scenario]:
        role = scenario[scenario_fields["role"]]
        production_request = scenario.get(scenario_fields["productionRequest"])
        execution_mode = scenario.get(scenario_fields["executionMode"])
        if not isinstance(production_request, dict) or execution_mode not in set(
            contract["executionModes"].values()
        ):
            result: ProductionResult | None = None
            lineage = None
            scenario_error = errors["invalidRequest"]
        else:
            try:
                workflow_adapters = (
                    _core_live_workflow_adapter(adapters, scenario, contract)
                    if execution_mode
                    == contract["executionModes"]["liveExternal"]
                    else adapters.workflow_adapters_for_scenario(
                        copy.deepcopy(scenario)
                    )
                )
                candidate = run_production(
                    copy.deepcopy(production_request), production_root, workflow_adapters
                )
            except Exception:
                candidate = None
            result = candidate if isinstance(candidate, ProductionResult) else None
            lineage = (
                _lineage_sha_by_role(result.output_dir, contract)
                if result is not None and result.outcome == "completed"
                else None
            )
            scenario_error = (
                None
                if lineage is not None
                else errors[
                    "lineageIncomplete"
                    if result is not None and result.outcome == "completed"
                    else "productionFailure"
                ]
            )
            if scenario_error is None:
                scenario_error = _scenario_evidence_error(
                    scenario=scenario,
                    result=result,
                    lineage=lineage,
                    corpus_item=(
                        corpus_items.get(role)
                        if corpus_items is not None
                        else None
                    ),
                    rules=rules,
                    contract=contract,
                )
            if (
                scenario_error is None
                and execution_mode
                == contract["executionModes"]["liveExternal"]
                and not _live_execution_evidence_valid(
                    result.output_dir, lineage, rules
                )
            ):
                scenario_error = errors["liveEvidenceMismatch"]
        formal_sha: str | None = None
        template_test_result: TemplateTestResult | None = None
        template_test_sha: str | None = None
        if scenario_error is None and result is not None:
            formal_path = _lineage_artifact_path(
                result.output_dir, "formalTemplate", contract
            )
            formal_sha = _sha_file(formal_path)
            if role in sampled_roles:
                formal = _load_object(formal_path)
                template_test_request = (
                    _template_test_request(
                        role=role,
                        production_output=result.output_dir,
                        template=formal,
                        rules=rules,
                        readiness_contract=contract,
                    )
                    if formal is not None
                    else None
                )
                if template_test_request is None:
                    scenario_error = errors["templateTestFailure"]
                else:
                    try:
                        test_adapters = (
                            _core_live_workflow_adapter(adapters, scenario, contract)
                            if execution_mode
                            == contract["executionModes"]["liveExternal"]
                            else adapters.template_test_adapters_for_scenario(
                                copy.deepcopy(scenario)
                            )
                        )
                        template_test_result = run_template_test(
                            template_test_request,
                            template_test_root,
                            test_adapters,
                        )
                    except Exception:
                        template_test_result = None
                    if not (
                        isinstance(template_test_result, TemplateTestResult)
                        and template_test_result.outcome == "completed"
                        and template_test_result.report_path is not None
                        and template_test_result.report_path.is_file()
                        and not template_test_result.report_path.is_symlink()
                        and _template_test_report_valid(
                            template_test_result.report_path,
                            template_test_request,
                            formal_sha,
                            rules,
                        )
                    ):
                        scenario_error = errors["templateTestFailure"]
                    else:
                        template_test_sha = _sha_file(template_test_result.report_path)
        if scenario_error is not None and overall_error is None:
            overall_error = scenario_error
        current_report = {
                scenario_report_fields["role"]: role,
                scenario_report_fields["productionItemId"]: (
                    result.production_item_id
                    if result is not None
                    else production_request.get("productionItemId")
                    if isinstance(production_request, dict)
                    else None
                ),
                scenario_report_fields["executionMode"]: execution_mode,
                scenario_report_fields["pass"]: scenario_error is None,
                scenario_report_fields["outcome"]: (
                    outcomes["passed"]
                    if scenario_error is None
                    else outcomes["failed"]
                ),
                scenario_report_fields["productionOutcome"]: (
                    result.outcome if result is not None else None
                ),
                scenario_report_fields["errorCode"]: (
                    scenario_error
                    if scenario_error is not None
                    else result.error_code if result is not None else None
                ),
                scenario_report_fields["outputDirectory"]: (
                    str(result.output_dir) if result is not None else None
                ),
                scenario_report_fields["lineageSha256ByRole"]: lineage or {},
                scenario_report_fields["formalTemplateSha256"]: formal_sha,
                scenario_report_fields["templateTestOutcome"]: (
                    template_test_result.outcome
                    if template_test_result is not None
                    else None
                ),
                scenario_report_fields["templateTestOutputDirectory"]: (
                    str(template_test_result.output_dir)
                    if template_test_result is not None
                    else None
                ),
                scenario_report_fields["templateTestReportSha256"]: template_test_sha,
            }
        if role == contract["forwardScenarioRole"]:
            forward_report = current_report
        else:
            scenario_reports.append(current_report)

    if verified_release_gates is not None:
        postflight_release_gates = _verified_release_gates(
            release_gate_evidence,
            corpus_items=corpus_items,
            rules=rules,
            contract=contract,
        )
        if postflight_release_gates != verified_release_gates:
            verified_release_gates = None
            if overall_error is None:
                overall_error = errors["releaseGateIncomplete"]
        else:
            expected_pin_sha = verified_release_gates[
                contract["releaseGateFields"]["runtimePinSha256"]
            ]
            scenario_pin_shas = [
                item.get(scenario_report_fields["lineageSha256ByRole"], {}).get(
                    "productionPin"
                )
                for item in [*scenario_reports, forward_report]
                if isinstance(item, dict)
            ]
            if len(scenario_pin_shas) != len(required_roles) + 1 or any(
                digest != expected_pin_sha for digest in scenario_pin_shas
            ):
                verified_release_gates = None
                if overall_error is None:
                    overall_error = errors["releaseGateIncomplete"]

    live_modes = [
        scenario.get(scenario_fields["executionMode"])
        for scenario in [*scenarios, forward_scenario]
    ]
    preflight = live_release_readiness_preflight()
    external_fields = contract["externalExecutionFields"]
    external_statuses = contract["externalExecutionStatuses"]
    all_live = all(
        mode == contract["executionModes"]["liveExternal"] for mode in live_modes
    )
    if all_live:
        external_execution = copy.deepcopy(preflight)
        if overall_error is None and preflight[external_fields["status"]] == external_statuses["ready"]:
            external_execution[external_fields["status"]] = external_statuses["completed"]
        elif overall_error is not None:
            external_execution[external_fields["status"]] = external_statuses["failed"]
    else:
        external_execution = {
            external_fields["status"]: external_statuses["recordedReplayOnly"],
            external_fields["missingCredentialRoles"]: [],
        }
    release_gates = (
        copy.deepcopy(verified_release_gates)
        if verified_release_gates is not None
        else _empty_release_gates(contract)
    )
    release_eligible = bool(
        overall_error is None
        and all_live
        and external_execution[external_fields["status"]]
        == external_statuses["completed"]
        and _release_gates_valid(release_gates, contract)
    )
    report = {
        report_fields["artifactType"]: contract["artifactType"],
        report_fields["schemaVersion"]: rules["schemaVersion"],
        report_fields["requestSha256"]: request_ledger[
            contract["requestLedgerFields"]["requestSha256"]
        ],
        report_fields["corpusSha256"]: request_ledger[
            contract["requestLedgerFields"]["corpusSha256"]
        ],
        report_fields["pass"]: overall_error is None,
        report_fields["outcome"]: (
            outcomes["passed"] if overall_error is None else outcomes["failed"]
        ),
        report_fields["errorCode"]: overall_error,
        report_fields["scenarios"]: scenario_reports,
        report_fields["forwardScenario"]: forward_report,
        report_fields["templateTestScenarioRoles"]: sampled_roles,
        report_fields["externalExecution"]: external_execution,
        report_fields["releaseGates"]: release_gates,
        report_fields["releaseEligible"]: release_eligible,
        report_fields["reportPath"]: str(report_path),
    }
    completion = _report_completion(
        report, request_ledger, rules=rules, contract=contract
    )
    if not _publish_report(report_path, report) or not _publish_report(
        completion_path, completion
    ):
        report[report_fields["pass"]] = False
        report[report_fields["outcome"]] = outcomes["failed"]
        report[report_fields["errorCode"]] = errors["reportConflict"]
        report[report_fields["releaseEligible"]] = False
    return report
