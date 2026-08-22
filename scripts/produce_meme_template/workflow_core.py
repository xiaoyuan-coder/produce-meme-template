from __future__ import annotations

import copy
import io
import json
import os
import re
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image, UnidentifiedImageError

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json as _load_json,
    pretty_json_bytes as _json_bytes,
    sha256_bytes as _sha_bytes,
    sha256_file as _sha_file,
)
from .validation import is_safe_public_https_url, is_valid_https_url


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "contracts" / "machine-rules.json"
RELEASE_PATH = REPO_ROOT / "release.json"
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISIBLE_TEXT_LEXEME = re.compile(
    r"[A-Za-z]+(?:['’][A-Za-z]+)*|\d+|[\u3400-\u4dbf\u4e00-\u9fff]{2,}"
)
MACHINE_RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))
GALLERY_SCHEMA_PATH = (
    REPO_ROOT
    / MACHINE_RULES["releaseManagementContract"]["gallerySnapshotRelativePath"]
)
GALLERY_SCHEMA = json.loads(GALLERY_SCHEMA_PATH.read_text(encoding="utf-8"))
BATCH_PRODUCTION_CONTRACT = MACHINE_RULES["batchProductionContract"]
INPUT_ID_PATTERN = GALLERY_SCHEMA["$defs"]["inputId"]["pattern"]
SUBJECT_IMAGE_MAX_COUNT = GALLERY_SCHEMA["$defs"]["subjectImageConfig"]["properties"][
    "maxCount"
]["maximum"]
INPUT_ID_PATTERN_BODY = INPUT_ID_PATTERN.removeprefix("^").removesuffix("$")
SLOT_ID = re.compile(INPUT_ID_PATTERN)
PLACEHOLDER = re.compile(
    r"\{\{\s*(" + INPUT_ID_PATTERN_BODY + r")(?=\s*(?:\||\}\}))[^}]*\}\}"
)
PLACEHOLDER_WITH_DEFAULT = re.compile(
    r'\{\{\s*(' + INPUT_ID_PATTERN_BODY + r')\s*\|\s*"([^"]*)"\s*\}\}'
)


def accepted_production_execution_modes(
    rules: dict[str, Any] = MACHINE_RULES,
) -> frozenset[str]:
    """Return every mode that carries a current execution identity."""

    contract = rules["productionExecutionContract"]
    return frozenset(
        (*contract["executionModes"].values(), contract["liveReadinessExecutionMode"])
    )


class WorkflowAdapters(Protocol):
    def resolve_template_identity(
        self, source_image: Path, request: dict[str, Any]
    ) -> dict[str, Any]: ...
    def analyze_source(
        self, source_image: Path, replacement_strategy: dict[str, Any] | None
    ) -> dict[str, Any]: ...
    def submit_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
    ) -> dict[str, Any]: ...
    def poll_generation(
        self,
        source_image: Path,
        generation_package: dict[str, Any],
        generation_task: dict[str, Any],
        submission: dict[str, Any],
    ) -> dict[str, Any]: ...
    def inspect_generated(
        self, generated_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]: ...
    def analyze_approved(self, approved_image: Path) -> dict[str, Any]: ...
    def analyze_approved_with_handoff(
        self, approved_image: Path, authoring_handoff: dict[str, Any]
    ) -> dict[str, Any]: ...
    def audit_authoring_contract(
        self, approved_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]: ...
    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]: ...
    def audit_visual_contract(
        self, approved_image: Path, review_request: dict[str, Any]
    ) -> dict[str, Any]: ...
    def upload(self, approved_image: Path, object_key: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ProductionResult:
    outcome: str
    production_item_id: str
    state: str
    output_dir: Path
    gallery_template: Path | None = None
    error_code: str | None = None
    message: str | None = None
    resumed: bool = False
    major_stage: str | None = None
    primary_artifact: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        stage_fields = MACHINE_RULES["majorStageContract"]["resultFields"]
        return {
            "outcome": self.outcome,
            "productionItemId": self.production_item_id,
            "state": self.state,
            "outputDir": str(self.output_dir),
            "galleryTemplate": str(self.gallery_template) if self.gallery_template else None,
            "errorCode": self.error_code,
            "message": self.message,
            "resumed": self.resumed,
            stage_fields["majorStage"]: self.major_stage,
            stage_fields["primaryArtifact"]: (
                str(self.primary_artifact) if self.primary_artifact else None
            ),
        }


@dataclass(frozen=True)
class BatchProductionResult:
    batch_id: str
    items: tuple[ProductionResult, ...]
    shared_policy_applied: bool
    error_code: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        fields = BATCH_PRODUCTION_CONTRACT["resultFields"]
        return {
            fields["batchIdentity"]: self.batch_id,
            fields["sharedPolicyApplied"]: self.shared_policy_applied,
            fields["items"]: [item.as_dict() for item in self.items],
            fields["errorCode"]: self.error_code,
            fields["message"]: self.message,
        }


class WorkflowStop(Exception):
    def __init__(self, outcome: str, state: str, error_code: str, message: str, evidence: dict[str, Any]):
        super().__init__(message)
        self.outcome = outcome
        self.state = state
        self.error_code = error_code
        self.message = message
        self.evidence = evidence


def _stop(
    rules: dict[str, Any], outcome: str, error_key: str, message: str, evidence: dict[str, Any]
) -> WorkflowStop:
    return WorkflowStop(
        outcome,
        rules["resultStates"][outcome],
        rules["errorCodes"][error_key],
        message,
        evidence,
    )


def _file_matches_sha(path: Path, expected_sha256: str) -> bool:
    try:
        return path.is_file() and _sha_file(path) == expected_sha256
    except OSError:
        return False


def _normalized_identity(value: str, modifiers: list[str]) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "S", "Z"))
    )
    normalized_modifiers = sorted(
        {
            "".join(
                character
                for character in unicodedata.normalize("NFKC", modifier).casefold()
                if not character.isspace()
                and not unicodedata.category(character).startswith(("P", "S", "Z"))
            )
            for modifier in modifiers
        },
        key=len,
        reverse=True,
    )
    changed = True
    while changed and normalized:
        changed = False
        for modifier in normalized_modifiers:
            if modifier and normalized.startswith(modifier):
                normalized = normalized[len(modifier):]
                changed = True
            if modifier and normalized.endswith(modifier):
                normalized = normalized[:-len(modifier)]
                changed = True
    return normalized


def _atomic_write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_file() and path.read_bytes() == data:
                return
            rules = _load_json(RULES_PATH)
            raise _stop(
                rules,
                "blocked",
                "immutableConflict",
                f"不可变产物已存在且内容不同：{path.name}",
                {"path": str(path)},
            )
    finally:
        temporary.unlink(missing_ok=True)


def _write_mutable_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _write_generation_wal(
    path: Path, wal: dict[str, Any], rules: dict[str, Any]
) -> None:
    previous_field = rules["generationExecutionContract"]["walFields"][
        "previousWalSha256"
    ]
    wal[previous_field] = _sha_file(path) if path.is_file() else None
    _write_mutable_json(path, wal)


def _deep_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for child in value.values():
            result.extend(_deep_strings(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_deep_strings(child))
        return result
    return []


def _deep_string_items(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        result: list[tuple[tuple[Any, ...], str]] = []
        for key, child in value.items():
            result.extend(_deep_string_items(child, (*path, key)))
        return result
    if isinstance(value, list):
        result = []
        for index, child in enumerate(value):
            result.extend(_deep_string_items(child, (*path, index)))
        return result
    return []


def _deep_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_deep_keys(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_deep_keys(v) for v in value), set())
    return set()


def _forbidden_formal_values(record: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    contract = rules["formalProjection"]
    patterns = contract["forbiddenValuePatterns"].values()
    top_level = contract["topLevel"]
    asset_fields = {top_level["coverAsset"], top_level["referenceAsset"]}
    return sorted(
        value
        for path, value in _deep_string_items(record)
        if not (
            len(path) == 1
            and path[0] in asset_fields
            and is_valid_https_url(value)
        )
        and any(re.search(pattern, value) for pattern in patterns)
    )


def _production_item_integrity_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    *,
    production_item_id: str,
    template_key: str,
    source_sha256: str,
    replacement_strategy_sha256: str,
    generation_options_sha256: str,
    required_artifacts: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    expected_identity = {
        "productionItemId": production_item_id,
        "templateKey": template_key,
        "sourceImageSha256": source_sha256,
        "replacementStrategySha256": replacement_strategy_sha256,
        "generationOptionsSha256": generation_options_sha256,
    }
    for field, expected in expected_identity.items():
        if manifest.get(field) != expected:
            errors.append(f"{field} mismatch")
    errors.extend(_revision_integrity_errors(manifest))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return [*errors, "artifact lineage missing"]
    for required in required_artifacts:
        if required not in artifacts:
            errors.append(f"{required} missing from lineage")
    resolved_root = output_dir.resolve()
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            errors.append(f"{name} artifact record invalid")
            continue
        if artifact.get("phase") not in {
            item["phase"] for item in MACHINE_RULES["productionPhases"]
        }:
            errors.append(f"{name} artifact phase invalid")
        dependencies = artifact.get("dependsOn")
        if not isinstance(dependencies, list) or not all(
            isinstance(dependency, str) and dependency for dependency in dependencies
        ):
            errors.append(f"{name} dependencies invalid")
        else:
            for dependency in dependencies:
                if dependency not in artifacts:
                    errors.append(f"{name} dependency missing: {dependency}")
            dependency_digest_field = BATCH_PRODUCTION_CONTRACT[
                "dependencyDigestField"
            ]
            dependency_digests = artifact.get(dependency_digest_field)
            immutable_dependencies = _immutable_dependency_names(dependencies)
            if not isinstance(dependency_digests, dict) or set(
                dependency_digests
            ) != set(immutable_dependencies):
                errors.append(f"{name} dependency digests invalid")
            else:
                for dependency in immutable_dependencies:
                    dependency_record = artifacts.get(dependency)
                    if (
                        not isinstance(dependency_record, dict)
                        or dependency_digests.get(dependency)
                        != dependency_record.get("sha256")
                    ):
                        errors.append(
                            f"{name} dependency digest mismatch: {dependency}"
                        )
        artifact_relative_path = artifact.get("path", name)
        if not isinstance(artifact_relative_path, str) or not artifact_relative_path:
            errors.append(f"{name} path invalid")
            continue
        artifact_path = (resolved_root / artifact_relative_path).resolve()
        if not artifact_path.is_relative_to(resolved_root):
            errors.append(f"{name} escapes production item")
            continue
        if not artifact_path.is_file():
            errors.append(f"{name} missing")
            continue
        if artifact.get("bytes") != artifact_path.stat().st_size:
            errors.append(f"{name} byte count mismatch")
        observed_sha = _sha_file(artifact_path)
        if observed_sha != artifact.get("sha256"):
            errors.append(f"{name} digest mismatch")
        scope_field = BATCH_PRODUCTION_CONTRACT["artifactScopeDigestField"]
        expected_scope_sha = _sha_bytes(
            _canonical_bytes(
                {
                    "productionItemId": production_item_id,
                    "artifact": name,
                    "sha256": observed_sha,
                }
            )
        )
        if artifact.get(scope_field) != expected_scope_sha:
            errors.append(f"{name} production item scope mismatch")
    return errors


def validate_production_manifest_lineage(
    output_dir: Path, manifest: Any
) -> list[str]:
    """Replay the workflow-owned lineage contract for any persisted item."""

    if not isinstance(manifest, dict):
        return ["production manifest must be an object"]
    identity_fields = {
        "productionItemId": manifest.get("productionItemId"),
        "templateKey": manifest.get("templateKey"),
        "sourceImageSha256": manifest.get("sourceImageSha256"),
        "replacementStrategySha256": manifest.get(
            "replacementStrategySha256"
        ),
        "generationOptionsSha256": manifest.get(
            "generationOptionsSha256"
        ),
    }
    execution_manifest_fields = MACHINE_RULES["productionExecutionContract"][
        "manifestFields"
    ]
    execution_mode = manifest.get(execution_manifest_fields["executionMode"])
    execution_sha = manifest.get(
        execution_manifest_fields["executionProfileSha256"]
    )
    legacy_execution_identity = execution_mode is None and execution_sha is None
    current_execution_identity = bool(
        execution_mode in accepted_production_execution_modes()
        and isinstance(execution_sha, str)
        and re.fullmatch(r"[0-9a-f]{64}", execution_sha)
    )
    if not (
        manifest.get("artifactType") == "production-manifest"
        and (legacy_execution_identity or current_execution_identity)
        and all(
            isinstance(value, str) and value.strip()
            for value in identity_fields.values()
        )
        and all(
            re.fullmatch(r"[0-9a-f]{64}", identity_fields[field])
            for field in (
                "sourceImageSha256",
                "replacementStrategySha256",
                "generationOptionsSha256",
            )
        )
    ):
        return ["production manifest identity invalid"]
    if current_execution_identity:
        execution_name = MACHINE_RULES["productionExecutionContract"]["artifactName"]
        execution_record = manifest.get("artifacts", {}).get(execution_name)
        if (
            not isinstance(execution_record, dict)
            or execution_record.get("sha256") != execution_sha
        ):
            return ["production execution profile lineage invalid"]
    phases = {
        item["phase"]: item["state"]
        for item in MACHINE_RULES["productionPhases"]
    }
    phase = manifest.get("phase")
    state = manifest.get("state")
    outcome = manifest.get("outcome")
    history = manifest.get("history")
    result_states = MACHINE_RULES["resultStates"]
    if not isinstance(history, list) or not all(
        isinstance(item, dict)
        and set(item) == {"phase", "state", "at"}
        and item.get("phase") in phases
        and item.get("state") == phases[item["phase"]]
        and isinstance(item.get("at"), str)
        and item["at"].strip()
        for item in history
    ):
        return ["production manifest history invalid"]
    if phase is None:
        if history:
            return ["production manifest phase/history mismatch"]
    elif phase not in phases or not history or history[-1]["phase"] != phase:
        return ["production manifest phase/history mismatch"]
    if outcome is None:
        expected_state = (
            MACHINE_RULES["initialState"] if phase is None else phases[phase]
        )
        if state != expected_state:
            return ["production manifest active state mismatch"]
    elif outcome not in result_states or state != result_states[outcome]:
        return ["production manifest outcome mismatch"]
    elif outcome == "completed" and phase != MACHINE_RULES[
        "productionPhases"
    ][-1]["phase"]:
        return ["completed production manifest phase mismatch"]
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return ["artifact lineage missing"]
    artifact_phases = {
        artifact.get("phase")
        for artifact in artifacts.values()
        if isinstance(artifact, dict)
    }
    if not {item["phase"] for item in history} <= artifact_phases:
        return ["production manifest phase artifacts missing"]
    return _production_item_integrity_errors(
        output_dir,
        manifest,
        production_item_id=identity_fields["productionItemId"],
        template_key=identity_fields["templateKey"],
        source_sha256=identity_fields["sourceImageSha256"],
        replacement_strategy_sha256=identity_fields[
            "replacementStrategySha256"
        ],
        generation_options_sha256=identity_fields[
            "generationOptionsSha256"
        ],
        required_artifacts=("production-pin.json",),
    )


def _revision_integrity_errors(manifest: dict[str, Any]) -> list[str]:
    revision = manifest.get("revision")
    artifacts = manifest.get("artifacts")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return ["manifest revision must be a positive integer"]
    if not isinstance(artifacts, dict):
        return []
    errors: list[str] = []
    artifact_revisions: list[int] = []
    revisioned_name_pattern = re.compile(r"-r([1-9][0-9]*)$")
    revision_one_names = re.compile(
        r"^(?:production-pin|generation-package|generation-task|generation-wal|visual-review|evidence/(?:generated-candidate-image|approved-template-image))\.[a-z0-9]+$"
    )
    for name, artifact in artifacts.items():
        artifact_revision = artifact.get("revision") if isinstance(artifact, dict) else None
        if (
            not isinstance(artifact_revision, int)
            or isinstance(artifact_revision, bool)
            or artifact_revision < 1
            or artifact_revision > revision
        ):
            errors.append(f"{name} artifact revision invalid")
            continue
        artifact_revisions.append(artifact_revision)
        name_revision = revisioned_name_pattern.search(Path(name).stem)
        if name_revision is not None and int(name_revision.group(1)) != artifact_revision:
            errors.append(f"{name} filename revision mismatch")
        if revision_one_names.fullmatch(name) and artifact_revision != 1:
            errors.append(f"{name} base filename revision mismatch")
    if artifact_revisions and max(artifact_revisions) != revision:
        errors.append("manifest revision does not match artifact lineage")
    return errors


def current_workflow_qualification_errors(
    output_dir: Path,
    manifest: Any,
) -> list[str]:
    """Replay manifest lineage and current P2 artifact bindings."""

    if not isinstance(manifest, dict):
        return ["production manifest must be an object"]
    return [
        *validate_production_manifest_lineage(output_dir, manifest),
        *_current_p2_artifact_errors(manifest),
    ]


def _record_artifact(
    manifest: dict[str, Any], output_dir: Path, name: str, phase: str, dependencies: list[str]
) -> None:
    path = output_dir / name
    artifact_sha = _sha_file(path)
    immutable_dependencies = _immutable_dependency_names(dependencies)
    manifest["artifacts"][name] = {
        "path": name,
        "sha256": artifact_sha,
        "bytes": path.stat().st_size,
        "phase": phase,
        "revision": manifest["revision"],
        "dependsOn": dependencies,
        BATCH_PRODUCTION_CONTRACT["dependencyDigestField"]: {
            dependency: manifest["artifacts"][dependency]["sha256"]
            for dependency in immutable_dependencies
        },
        BATCH_PRODUCTION_CONTRACT["artifactScopeDigestField"]: _sha_bytes(
            _canonical_bytes(
                {
                    "productionItemId": manifest["productionItemId"],
                    "artifact": name,
                    "sha256": artifact_sha,
                }
            )
        ),
    }


def _immutable_dependency_names(dependencies: list[str]) -> list[str]:
    artifact_types = MACHINE_RULES["generationExecutionContract"]["artifactTypes"]
    mutable_prefixes = tuple(
        artifact_types[role]
        for role in BATCH_PRODUCTION_CONTRACT[
            "mutableDependencyArtifactTypeRoles"
        ]
    )
    return [
        dependency
        for dependency in dependencies
        if not Path(dependency).stem.startswith(mutable_prefixes)
    ]


def _artifact_descendants(manifest: dict[str, Any], root_name: str) -> list[str]:
    descendants: set[str] = set()
    artifacts = manifest.get("artifacts", {})
    changed = True
    while changed:
        changed = False
        for name, artifact in artifacts.items():
            if not isinstance(artifact, dict):
                continue
            raw_dependencies = artifact.get("dependsOn")
            if not isinstance(raw_dependencies, list) or not all(
                isinstance(dependency, str) for dependency in raw_dependencies
            ):
                continue
            dependencies = set(raw_dependencies)
            if name not in descendants and (root_name in dependencies or dependencies & descendants):
                descendants.add(name)
                changed = True
    return sorted(descendants)


def _revision_image_artifacts(
    manifest: dict[str, Any], role: str, revision: int
) -> list[str]:
    pattern = re.compile(rf"^evidence/{re.escape(role)}(?:-r[1-9][0-9]*)?\.[a-z0-9]+$")
    return sorted(
        name
        for name, artifact in manifest.get("artifacts", {}).items()
        if pattern.fullmatch(name)
        and isinstance(artifact, dict)
        and artifact.get("revision") == revision
    )


def _current_p2_artifact_errors(manifest: dict[str, Any]) -> list[str]:
    revision = manifest.get("revision")
    artifacts = manifest.get("artifacts")
    if not isinstance(revision, int) or isinstance(revision, bool) or not isinstance(artifacts, dict):
        return []
    errors: list[str] = []
    for name in (
        MACHINE_RULES["authoringHandoffContract"]["artifactNames"]["handoff"],
        _revisioned_name("generation-package.json", revision),
        _revisioned_name("generation-task.json", revision),
        _revisioned_name("generation-wal.json", revision),
        _revisioned_name("visual-review.json", revision),
    ):
        if name not in artifacts:
            errors.append(f"current P2 artifact missing: {name}")
    for role in ("generated-candidate-image", "approved-template-image"):
        names = _revision_image_artifacts(manifest, role, revision)
        if len(names) != 1:
            errors.append(f"current P2 {role} count must be one")
    return errors


def _changed_lineage_artifacts(
    output_dir: Path,
    manifest: dict[str, Any],
    names: list[str],
) -> list[str]:
    changed: list[str] = []
    for name in names:
        artifact = manifest.get("artifacts", {}).get(name)
        if not isinstance(artifact, dict):
            continue
        artifact_relative_path = artifact.get("path", name)
        if not isinstance(artifact_relative_path, str) or not artifact_relative_path:
            continue
        artifact_path = output_dir / artifact_relative_path
        if not artifact_path.is_file() or _sha_file(artifact_path) != artifact.get("sha256"):
            changed.append(name)
    return changed


def _append_invalidation_event(
    manifest: dict[str, Any],
    rules: dict[str, Any],
    *,
    reason_key: str,
    superseded_artifact: str,
    invalidated_artifacts: list[str],
    invalidated_from_phase: str,
    timestamp: str,
    observed_sha256: str | None = None,
    replacement_artifact: str | None = None,
    replacement_sha256: str | None = None,
) -> None:
    phase_index = next(
        index
        for index, item in enumerate(rules["productionPhases"])
        if item["phase"] == invalidated_from_phase
    )
    superseded = manifest["artifacts"][superseded_artifact]
    event: dict[str, Any] = {
        "revision": manifest["revision"],
        "reason": rules["invalidationReasons"][reason_key],
        "invalidatedAt": timestamp,
        "supersededArtifact": superseded_artifact,
        "supersededSha256": superseded["sha256"],
        "invalidatedArtifacts": invalidated_artifacts,
        "invalidatedPhases": [
            item["phase"] for item in rules["productionPhases"][phase_index:]
        ],
    }
    if observed_sha256 is not None:
        event["observedSha256"] = observed_sha256
    if replacement_artifact is not None and replacement_sha256 is not None:
        event["replacementArtifact"] = replacement_artifact
        event["replacementSha256"] = replacement_sha256
    events = manifest.setdefault("invalidationEvents", [])
    identity = (
        event["revision"],
        event["reason"],
        event["supersededArtifact"],
        event.get("observedSha256"),
        event.get("replacementSha256"),
    )
    if not any(
        (
            item.get("revision"),
            item.get("reason"),
            item.get("supersededArtifact"),
            item.get("observedSha256"),
            item.get("replacementSha256"),
        )
        == identity
        for item in events
    ):
        events.append(event)


def _revisioned_name(name: str, revision: int) -> str:
    if revision == 1:
        return name
    path = Path(name)
    return str(path.with_name(f"{path.stem}-r{revision}{path.suffix}"))


def revisioned_artifact_name(name: str, revision: int) -> str:
    """Return the workflow-owned filename for an artifact revision."""

    return _revisioned_name(name, revision)


def _advance(manifest: dict[str, Any], rules: dict[str, Any], phase: str, timestamp: str) -> None:
    states = {item["phase"]: item["state"] for item in rules["productionPhases"]}
    if phase not in states:
        raise ValueError(f"未知生产阶段：{phase}")
    state = states[phase]
    manifest["phase"] = phase
    manifest["state"] = state
    manifest["history"].append({"phase": phase, "state": state, "at": timestamp})


def _persist_manifest(output_dir: Path, manifest: dict[str, Any]) -> None:
    _write_mutable_json(output_dir / "production-manifest.json", manifest)


def _adapter_call(rules: dict[str, Any], operation: str, function: Callable[..., Any], *args: Any) -> Any:
    try:
        return function(*args)
    except Exception as exc:
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            f"外部适配器执行失败：{operation}",
            {
                "operation": operation,
                "exceptionType": type(exc).__name__,
                "detailSha256": _sha_bytes(str(exc).encode("utf-8")),
            },
        ) from exc


def _adapter_object_call(
    rules: dict[str, Any], operation: str, function: Callable[..., Any], *args: Any
) -> dict[str, Any]:
    result = _adapter_call(rules, operation, function, *args)
    if not isinstance(result, dict):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            f"外部适配器必须返回对象：{operation}",
            {"operation": operation, "actualType": type(result).__name__},
        )
    return result


def _adapter_readonly_image_object_call(
    rules: dict[str, Any],
    operation: str,
    function: Callable[..., Any],
    image_path: Path,
    expected_sha256: str,
    *args: Any,
) -> dict[str, Any]:
    if not _file_matches_sha(image_path, expected_sha256):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            f"外部适配器输入图片摘要已失效：{operation}",
            {"operation": operation, "expectedImageSha256": expected_sha256},
        )
    original_mode = image_path.stat().st_mode & 0o777
    image_path.chmod(original_mode & ~0o222)
    try:
        result = _adapter_object_call(rules, operation, function, image_path, *args)
    finally:
        if image_path.is_file() and not image_path.is_symlink():
            image_path.chmod(original_mode)
    if (
        not image_path.is_file()
        or image_path.is_symlink()
        or _sha_file(image_path) != expected_sha256
    ):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            f"外部适配器修改了只读图片：{operation}",
            {"operation": operation, "expectedImageSha256": expected_sha256},
        )
    return result


def _adapter_snapshot_image_object_call(
    rules: dict[str, Any],
    operation: str,
    function: Callable[..., Any],
    source_image: Path,
    expected_sha256: str,
    *args: Any,
) -> dict[str, Any]:
    if not _file_matches_sha(source_image, expected_sha256):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            f"外部适配器调用前来源图片摘要已失效：{operation}",
            {"operation": operation, "expectedImageSha256": expected_sha256},
        )
    with tempfile.TemporaryDirectory(prefix=f"{operation}-image-") as adapter_directory:
        snapshot = Path(adapter_directory) / source_image.name
        snapshot.write_bytes(source_image.read_bytes())
        result = _adapter_readonly_image_object_call(
            rules,
            operation,
            function,
            snapshot,
            expected_sha256,
            *args,
        )
    if not _file_matches_sha(source_image, expected_sha256):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            f"外部适配器调用期间来源图片摘要发生变化：{operation}",
            {"operation": operation, "expectedImageSha256": expected_sha256},
        )
    return result


def _public_asset_url_valid(value: Any, rules: dict[str, Any]) -> bool:
    if not is_safe_public_https_url(value):
        return False
    parsed = urlsplit(value)
    policy = rules["objectStorageContract"]["assetUrlPolicy"]
    return bool(
        (policy["allowQuery"] or not parsed.query)
        and (policy["allowFragment"] or not parsed.fragment)
    )
