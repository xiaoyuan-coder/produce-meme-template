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
from .release_management import doctor, runtime_production_pin
from .validation import is_safe_public_https_url, is_valid_https_url


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "contracts" / "machine-rules.json"
GALLERY_SCHEMA_PATH = (
    REPO_ROOT
    / "contracts"
    / "upstream"
    / "gallery-template"
    / "current-cover-contract"
    / "gallery-template.schema.json"
)
RELEASE_PATH = REPO_ROOT / "release.json"
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISIBLE_TEXT_LEXEME = re.compile(
    r"[A-Za-z]+(?:['’][A-Za-z]+)*|\d+|[\u3400-\u4dbf\u4e00-\u9fff]{2,}"
)
GALLERY_SCHEMA = json.loads(GALLERY_SCHEMA_PATH.read_text(encoding="utf-8"))
MACHINE_RULES = json.loads(RULES_PATH.read_text(encoding="utf-8"))
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


class WorkflowAdapters(Protocol):
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
    def audit_semantics(self, content: dict[str, Any]) -> dict[str, Any]: ...
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "productionItemId": self.production_item_id,
            "state": self.state,
            "outputDir": str(self.output_dir),
            "galleryTemplate": str(self.gallery_template) if self.gallery_template else None,
            "errorCode": self.error_code,
            "message": self.message,
            "resumed": self.resumed,
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
    if not (
        manifest.get("artifactType") == "production-manifest"
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


from .batch_policy import (
    _replacement_strategy_errors,
    _generation_options_errors,
    _production_request_errors,
    _isolated_output_dir,
    _normalized_generation_options,
    _normalize_replacement_strategy,
    _shared_policy_errors,
    _normalize_shared_policy,
    _batch_priority,
    _source_analysis_identity_valid,
    _merge_shared_policy_strategy,
    _allocation_analysis_strategy,
    _allocation_candidate_evaluations,
    _allocation_preserve_evaluations,
    _batch_preserve_conflicts,
    _policy_resolution_valid,
    _shared_policy_plan_valid,
    _resolve_shared_policy,
)




from .replacement_planning import (
    _build_pin,
    _complete_typed_relation_chain,
    _component_graph_view,
    _identity_relations_are_consistent,
    _plan_replacement,
    _validated_source_multi_instance_contract,
)




from .generation_runtime import (
    _compile_generation_package,
    _compile_redo_generation_package,
    _compile_generation_task,
    _prepared_generation_wal,
    _generation_failure_stop,
    _sanitize_generation_failure_reason,
    _generation_submission_shape_valid,
    _execution_identity_valid,
    _generation_output_assets_valid,
    _image_bytes_match_output_format,
    image_bytes_match_output_format,
    _generation_poll_shape_valid,
    _generation_task_wal_errors,
    _load_generation_execution_evidence,
    _adopt_pre_submit_generation_staging,
    _current_generation_execution_errors,
    _evaluate_visual_gate,
)






def _delivery_image_context(
    output_dir: Path, manifest: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    revision = manifest.get("revision")
    candidate_names = _revision_image_artifacts(
        manifest, "generated-candidate-image", revision
    )
    approved_names = _revision_image_artifacts(
        manifest, "approved-template-image", revision
    )
    errors: list[str] = []
    if len(candidate_names) != 1:
        errors.append("current candidate image count must be one")
    if len(approved_names) != 1:
        errors.append("current approved image count must be one")
    if errors:
        return errors, {}
    candidate_name = candidate_names[0]
    approved_name = approved_names[0]
    candidate_path = output_dir / candidate_name
    approved_path = output_dir / approved_name
    if not candidate_path.is_file() or not approved_path.is_file():
        return ["current candidate or approved image missing"], {}
    candidate_sha = _sha_file(candidate_path)
    approved_sha = _sha_file(approved_path)
    if candidate_sha != approved_sha:
        errors.append("approved image no longer matches the reviewed candidate")
    return errors, {
        "candidateName": candidate_name,
        "candidateSha256": candidate_sha,
        "approvedName": approved_name,
        "approvedPath": approved_path,
        "approvedSha256": approved_sha,
    }


def _object_storage_key(
    template_key: str, approved_path: Path, approved_sha256: str, rules: dict[str, Any]
) -> str:
    contract = rules["objectStorageContract"]
    object_key = (
        f"{contract['objectKeyPrefix']}/{template_key}/"
        f"{approved_sha256}{approved_path.suffix.lower()}"
    )
    allowed_extensions = set(
        rules["generationExecutionContract"]["outputFormatExtensions"].values()
    )
    if (
        not re.fullmatch(r"[a-z][a-z0-9/-]*", contract["objectKeyPrefix"])
        or approved_path.suffix.lower() not in allowed_extensions
        or ".." in object_key
        or object_key.startswith("/")
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "OSS 对象键不符合冻结合同。",
            {"objectKey": object_key},
        )
    return object_key


def _public_asset_url_valid(value: Any, rules: dict[str, Any]) -> bool:
    if not is_safe_public_https_url(value):
        return False
    parsed = urlsplit(value)
    policy = rules["objectStorageContract"]["assetUrlPolicy"]
    return bool(
        (policy["allowQuery"] or not parsed.query)
        and (policy["allowFragment"] or not parsed.fragment)
    )


from .template_compiler import (
    _compile_draft,
    _compile_editable_spec,
    _compile_hidden_spec,
    _formal_projection,
    _resolve_prompt,
    _semantic_audit_payload,
    _validate_final,
    _validation_report,
    formal_template_contract_valid,
)


def _asset_url_matches_object_key(value: Any, object_key: str) -> bool:
    if not isinstance(value, str):
        return False
    path = unquote(urlsplit(value).path)
    return path == "/" + object_key or path.endswith("/" + object_key)


def _storage_identity_valid(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _upload_result_valid(
    result: Any,
    expected_object_key: str,
    expected_image_sha256: str,
    rules: dict[str, Any],
) -> bool:
    contract = rules["objectStorageContract"]
    fields = contract["adapterResultFields"]
    return bool(
        isinstance(result, dict)
        and set(result) == set(fields.values())
        and _storage_identity_valid(
            result.get(fields["provider"]), contract["providerIdentityPattern"]
        )
        and result.get(fields["provider"]) in contract["providerRoles"].values()
        and result.get(fields["objectKey"]) == expected_object_key
        and _storage_identity_valid(
            result.get(fields["objectIdentity"]),
            contract["remoteIdentityPattern"],
        )
        and result.get(fields["imageSha256"]) == expected_image_sha256
        and _public_asset_url_valid(result.get(fields["url"]), rules)
        and _asset_url_matches_object_key(
            result.get(fields["url"]), expected_object_key
        )
        and _storage_identity_valid(
            result.get(fields["idempotencyKey"]),
            contract["idempotencyIdentityPattern"],
        )
        and result.get(fields["idempotencyKey"])
        == contract["idempotencyKeyPrefix"] + expected_image_sha256
        and result.get(fields["uploadStatus"])
        in contract["uploadStatuses"].values()
        and _storage_identity_valid(
            result.get(fields["providerRequestIdentity"]),
            contract["requestIdentityPattern"],
        )
        and isinstance(result.get(fields["providerStatusCode"]), int)
        and not isinstance(result.get(fields["providerStatusCode"]), bool)
        and 200 <= result[fields["providerStatusCode"]] < 300
    )


def _build_asset_receipt(
    manifest: dict[str, Any],
    delivery: dict[str, Any],
    upload_result: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["objectStorageContract"]
    result_fields = contract["adapterResultFields"]
    receipt_fields = contract["receiptFields"]
    return {
        receipt_fields["artifactType"]: contract["artifactType"],
        receipt_fields["schemaVersion"]: rules["schemaVersion"],
        receipt_fields["productionItemIdentity"]: manifest["productionItemId"],
        receipt_fields["templateKey"]: manifest["templateKey"],
        receipt_fields["formalRevision"]: manifest["revision"],
        receipt_fields["candidateArtifact"]: delivery["candidateName"],
        receipt_fields["candidateImageSha256"]: delivery["candidateSha256"],
        receipt_fields["approvedArtifact"]: delivery["approvedName"],
        receipt_fields["approvedImageSha256"]: delivery["approvedSha256"],
        receipt_fields["provider"]: upload_result[result_fields["provider"]],
        receipt_fields["objectKey"]: upload_result[result_fields["objectKey"]],
        receipt_fields["objectIdentity"]: upload_result[
            result_fields["objectIdentity"]
        ],
        receipt_fields["url"]: upload_result[result_fields["url"]],
        receipt_fields["idempotencyKey"]: upload_result[
            result_fields["idempotencyKey"]
        ],
        receipt_fields["uploadStatus"]: upload_result[
            result_fields["uploadStatus"]
        ],
        receipt_fields["providerRequestIdentity"]: upload_result[
            result_fields["providerRequestIdentity"]
        ],
        receipt_fields["providerStatusCode"]: upload_result[
            result_fields["providerStatusCode"]
        ],
    }


def _asset_receipt_valid(
    receipt: Any,
    manifest: dict[str, Any],
    delivery: dict[str, Any],
    expected_object_key: str,
    rules: dict[str, Any],
) -> bool:
    contract = rules["objectStorageContract"]
    fields = contract["receiptFields"]
    return bool(
        isinstance(receipt, dict)
        and set(receipt) == set(fields.values())
        and receipt.get(fields["artifactType"]) == contract["artifactType"]
        and receipt.get(fields["schemaVersion"]) == rules["schemaVersion"]
        and receipt.get(fields["productionItemIdentity"])
        == manifest["productionItemId"]
        and receipt.get(fields["templateKey"]) == manifest["templateKey"]
        and receipt.get(fields["formalRevision"]) == manifest["revision"]
        and receipt.get(fields["candidateArtifact"]) == delivery["candidateName"]
        and receipt.get(fields["candidateImageSha256"])
        == delivery["candidateSha256"]
        and receipt.get(fields["approvedArtifact"]) == delivery["approvedName"]
        and receipt.get(fields["approvedImageSha256"])
        == delivery["approvedSha256"]
        and receipt.get(fields["objectKey"]) == expected_object_key
        and _storage_identity_valid(
            receipt.get(fields["provider"]), contract["providerIdentityPattern"]
        )
        and receipt.get(fields["provider"]) in contract["providerRoles"].values()
        and _storage_identity_valid(
            receipt.get(fields["objectIdentity"]), contract["remoteIdentityPattern"]
        )
        and _public_asset_url_valid(receipt.get(fields["url"]), rules)
        and _asset_url_matches_object_key(
            receipt.get(fields["url"]), expected_object_key
        )
        and _storage_identity_valid(
            receipt.get(fields["idempotencyKey"]),
            contract["idempotencyIdentityPattern"],
        )
        and receipt.get(fields["idempotencyKey"])
        == contract["idempotencyKeyPrefix"] + delivery["approvedSha256"]
        and receipt.get(fields["uploadStatus"])
        in contract["uploadStatuses"].values()
        and _storage_identity_valid(
            receipt.get(fields["providerRequestIdentity"]),
            contract["requestIdentityPattern"],
        )
        and isinstance(receipt.get(fields["providerStatusCode"]), int)
        and not isinstance(receipt.get(fields["providerStatusCode"]), bool)
        and 200 <= receipt[fields["providerStatusCode"]] < 300
    )


def _current_finalization_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
    if delivery_errors:
        return delivery_errors
    try:
        object_key = _object_storage_key(
            manifest["templateKey"],
            delivery["approvedPath"],
            delivery["approvedSha256"],
            rules,
        )
        receipt = _load_json(output_dir / "asset-receipt.json")
        draft = _load_json(output_dir / "gallery-template.draft.json")
        record = _load_json(output_dir / "gallery-template.json")
        persisted_validation = _load_json(
            output_dir / "final-validation-report.json"
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["finalization evidence unreadable"]
    if not all(
        isinstance(item, dict)
        for item in (receipt, draft, record, persisted_validation)
    ):
        return ["finalization evidence shape invalid"]
    if not _asset_receipt_valid(
        receipt, manifest, delivery, object_key, rules
    ):
        return ["asset receipt semantic binding invalid"]
    receipt_fields = rules["objectStorageContract"]["receiptFields"]
    try:
        expected_record = _formal_projection(
            draft, receipt[receipt_fields["url"]], rules
        )
    except WorkflowStop:
        return ["formal projection source invalid"]
    expected_validation = _validate_final(expected_record, rules)
    errors: list[str] = []
    if record != expected_record:
        errors.append("formal record does not match receipt projection")
    if persisted_validation != expected_validation or not expected_validation["pass"]:
        errors.append("final validation report does not match current formal record")
    return errors


def _current_item_fact_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    try:
        source_analysis = _load_json(output_dir / "source-analysis.json")
        plan = _load_json(output_dir / "replacement-plan.json")
        template_analysis = _load_json(output_dir / "template-analysis.json")
        editable = _load_json(output_dir / "editable-template-spec.json")
        hidden = _load_json(output_dir / "hidden-template-spec.json")
        draft = _load_json(output_dir / "gallery-template.draft.json")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["production item facts unreadable"]
    if not all(
        isinstance(item, dict)
        for item in (
            source_analysis,
            plan,
            template_analysis,
            editable,
            hidden,
            draft,
        )
    ):
        return ["production item facts shape invalid"]
    errors: list[str] = []
    if source_analysis.get("sourceImageSha256") != manifest.get(
        "sourceImageSha256"
    ):
        errors.append("source analysis belongs to another source image")
    if plan.get("templateKey") != manifest.get("templateKey"):
        errors.append("replacement plan belongs to another template")
    source_target = source_analysis.get("target")
    plan_targets = plan.get("primaryTargets")
    if not (
        isinstance(source_target, dict)
        and isinstance(plan_targets, list)
        and len(plan_targets) == 1
        and isinstance(plan_targets[0], dict)
        and plan_targets[0].get("sourceCategory") == source_target.get("category")
        and plan_targets[0].get("sourceRole") == source_target.get("role")
        and plan_targets[0].get("sourceIdentity") == source_target.get("identity")
    ):
        errors.append("replacement plan does not match source facts")
    batch_contract = rules["batchProductionContract"]
    resolution_name = batch_contract["resolutionArtifactName"]
    if resolution_name in manifest.get("artifacts", {}):
        try:
            resolution = _load_json(output_dir / resolution_name)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            errors.append("shared policy resolution unreadable")
        else:
            if not _shared_policy_plan_valid(plan, resolution, rules):
                errors.append(
                    "shared policy resolution does not match replacement plan"
                )
    delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
    if delivery_errors:
        errors.extend(delivery_errors)
    elif template_analysis.get("visualFactSourceSha256") != delivery.get(
        "approvedSha256"
    ):
        errors.append("template analysis belongs to another approved image")
    try:
        expected_editable = _compile_editable_spec(
            template_analysis,
            rules,
            plan,
        )
        expected_hidden = _compile_hidden_spec(
            template_analysis,
            expected_editable,
            rules,
        )
        expected_draft = _compile_draft(
            manifest["templateKey"],
            source_analysis.get("imageSize", "1024x1024"),
            expected_editable,
            expected_hidden,
            rules,
        )
    except (KeyError, TypeError, ValueError, WorkflowStop):
        errors.append("production item facts cannot be deterministically replayed")
    else:
        if editable != expected_editable:
            errors.append("editable defaults do not match approved visual facts")
        if hidden != expected_hidden:
            errors.append("hidden template does not match approved visual facts")
        if draft != expected_draft:
            errors.append("draft does not match current item compilation")
    return errors


def _current_shared_policy_resolution_errors(
    output_dir: Path,
    expected_resolution: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    resolution_name = rules["batchProductionContract"][
        "resolutionArtifactName"
    ]
    try:
        persisted = _load_json(output_dir / resolution_name)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["shared policy resolution unreadable"]
    if persisted != expected_resolution:
        return ["shared policy resolution does not match current request"]
    return []


def _finalize_uploaded_item(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
    timestamp: str,
) -> ProductionResult:
    try:
        draft = _load_json(output_dir / "gallery-template.draft.json")
        receipt = _load_json(output_dir / "asset-receipt.json")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 正式投影源或 Asset Receipt 无法读取。",
            {},
        )
    if not isinstance(draft, dict) or not isinstance(receipt, dict):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 正式投影源或 Asset Receipt 形状无效。",
            {},
        )
    delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
    if delivery_errors:
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 恢复要求唯一且摘要一致的候选图与确认模板图谱系。",
            {"errors": delivery_errors},
        )
    expected_object_key = _object_storage_key(
        manifest["templateKey"],
        delivery["approvedPath"],
        delivery["approvedSha256"],
        rules,
    )
    if not _asset_receipt_valid(
        receipt, manifest, delivery, expected_object_key, rules
    ):
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 Asset Receipt 与确认模板图或对象键不一致。",
            {"path": str(output_dir / "asset-receipt.json")},
        )

    receipt_fields = rules["objectStorageContract"]["receiptFields"]
    final_record = _formal_projection(draft, receipt[receipt_fields["url"]], rules)
    final_validation = _validate_final(final_record, rules)
    _atomic_write_new(output_dir / "final-validation-report.json", _json_bytes(final_validation))
    _record_artifact(
        manifest,
        output_dir,
        "final-validation-report.json",
        rules["productionPhases"][8]["phase"],
        ["gallery-template.draft.json", "asset-receipt.json"],
    )
    if not final_validation["pass"]:
        raise _stop(rules, "blocked", "contractFailure", "正式 JSON 最终合同验证未通过。", final_validation)
    _atomic_write_new(output_dir / "gallery-template.json", _json_bytes(final_record))
    _record_artifact(
        manifest,
        output_dir,
        "gallery-template.json",
        rules["productionPhases"][8]["phase"],
        ["gallery-template.draft.json", "asset-receipt.json", "final-validation-report.json"],
    )
    _advance(manifest, rules, rules["productionPhases"][8]["phase"], timestamp)
    manifest["outcome"] = "completed"
    _persist_manifest(output_dir, manifest)
    return ProductionResult(
        "completed",
        manifest["productionItemId"],
        rules["resultStates"]["completed"],
        output_dir,
        output_dir / "gallery-template.json",
        resumed=True,
    )


from .production_runtime import _run_single_production




def _run_batch_item(
    request: dict[str, Any],
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None,
    prepared_source_analysis: dict[str, Any] | None = None,
    shared_policy_resolution: dict[str, Any] | None = None,
    preparation_stop: WorkflowStop | None = None,
) -> ProductionResult:
    try:
        return _run_single_production(
            request,
            output_root,
            adapters,
            clock=clock,
            prepared_source_analysis=prepared_source_analysis,
            shared_policy_resolution=shared_policy_resolution,
            preparation_stop=preparation_stop,
        )
    except (KeyError, OSError, TypeError, ValueError):
        rules = _load_json(RULES_PATH)
        item_id = str(request.get("productionItemId", "invalid-production-item"))
        return ProductionResult(
            "needs_input",
            item_id,
            rules["resultStates"]["needs_input"],
            Path(output_root).resolve() / item_id,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="批量中该 Production Item 的输入无法读取或解析。",
        )


def run_production(
    request: Any,
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ProductionResult | BatchProductionResult:
    """Run one Production Item or split a batch into independent P0-P8 items."""

    rules = _load_json(RULES_PATH)
    if not isinstance(request, dict):
        return ProductionResult(
            "needs_input",
            "invalid-production-item",
            rules["resultStates"]["needs_input"],
            Path(output_root).resolve(),
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="生产请求必须是对象。",
        )
    contract = rules["batchProductionContract"]
    request_fields = contract["requestFields"]
    batch_field = request_fields["batchIdentity"]
    items_field = request_fields["items"]
    shared_policy_field = request_fields["sharedPolicy"]
    if batch_field not in request and items_field not in request:
        return _run_single_production(
            request, output_root, adapters, clock=clock
        )
    batch_id = request.get(batch_field)
    raw_items = request.get(items_field)
    item_ids = [
        item.get("productionItemId")
        for item in raw_items
        if isinstance(item, dict)
    ] if isinstance(raw_items, list) else []
    envelope_fields = {batch_field, items_field, shared_policy_field}
    if (
        not isinstance(batch_id, str)
        or re.fullmatch(rules["identifiers"]["productionItemIdPattern"], batch_id)
        is None
        or set(request) - envelope_fields
        or not isinstance(raw_items, list)
        or not contract["minimumItems"] <= len(raw_items) <= contract["maximumItems"]
        or not all(isinstance(item, dict) for item in raw_items)
        or not all(
            isinstance(item_id, str)
            and re.fullmatch(
                rules["identifiers"]["productionItemIdPattern"], item_id
            )
            is not None
            for item_id in item_ids
        )
        or len(item_ids) != len(set(item_ids))
    ):
        return BatchProductionResult(
            batch_id=batch_id if isinstance(batch_id, str) and batch_id else "invalid-batch",
            items=(),
            shared_policy_applied=False,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="批量请求的标识符或 Production Item 列表无效。",
        )
    shared_policy = request.get(shared_policy_field)
    if shared_policy is None:
        results = tuple(
            _run_batch_item(
                item, output_root, adapters, clock=clock
            )
            for item in raw_items
        )
        return BatchProductionResult(
            batch_id=batch_id,
            items=results,
            shared_policy_applied=False,
        )
    policy_errors = _shared_policy_errors(shared_policy, set(item_ids), rules)
    if policy_errors:
        return BatchProductionResult(
            batch_id=batch_id,
            items=(),
            shared_policy_applied=False,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="共享批次策略预检失败：" + "；".join(policy_errors),
        )
    normalized_policy = _normalize_shared_policy(shared_policy, rules)
    output_root_path = Path(output_root).resolve()
    schema = _load_json(GALLERY_SCHEMA_PATH)
    invalid_item_ids = {
        item["productionItemId"]
        for item in raw_items
        if _production_request_errors(item, rules, schema)
        or _isolated_output_dir(
            output_root_path,
            item["productionItemId"],
        )
        is None
    }
    try:
        (
            effective_requests,
            analyses,
            resolutions,
            preparation_failures,
        ) = _resolve_shared_policy(
            batch_id,
            raw_items,
            normalized_policy,
            output_root_path,
            adapters,
            rules,
            invalid_item_ids,
        )
    except WorkflowStop as stop:
        return BatchProductionResult(
            batch_id=batch_id,
            items=(),
            shared_policy_applied=True,
            error_code=stop.error_code,
            message=stop.message,
        )
    scope = set(
        normalized_policy[contract["sharedPolicyFields"]["scope"]]
    )
    item_results: list[ProductionResult] = []
    for item in raw_items:
        item_id = item["productionItemId"]
        if item_id not in scope:
            item_results.append(
                _run_batch_item(
                    item, output_root, adapters, clock=clock
                )
            )
            continue
        if item_id in preparation_failures:
            failed_request = effective_requests.get(item_id, item)
            item_results.append(
                _run_batch_item(
                    failed_request,
                    output_root,
                    adapters,
                    clock=clock,
                    preparation_stop=preparation_failures[item_id],
                )
            )
            continue
        if item_id not in effective_requests or item_id not in resolutions:
            item_results.append(
                _run_batch_item(
                    item,
                    output_root,
                    adapters,
                    clock=clock,
                    preparation_stop=_stop(
                        rules,
                        "blocked",
                        "noCompatibleReplacement",
                        "共享批次策略没有为该生产项分配兼容值。",
                        {"productionItemId": item_id},
                    ),
                )
            )
            continue
        item_results.append(
            _run_batch_item(
                effective_requests[item_id],
                output_root,
                adapters,
                clock=clock,
                prepared_source_analysis=analyses.get(item_id),
                shared_policy_resolution=resolutions[item_id],
            )
        )
    return BatchProductionResult(
        batch_id=batch_id,
        items=tuple(item_results),
        shared_policy_applied=True,
    )
