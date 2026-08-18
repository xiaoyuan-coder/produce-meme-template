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


def _compile_generation_package(
    plan: dict[str, Any], source_analysis: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    target = plan["primaryTargets"][0]
    dependency_value_field = rules["identityReplacementContract"]["dependencyFields"][
        "description"
    ]
    sections = {
        "task": "基于参考资产完成整图重构，输出一张可独立使用的新模板图。",
        "replacementTarget": f"将{target['sourceRole']}完整替换为{target['replacementValue']}。",
        "dependencyClosure": "；".join(
            item[dependency_value_field] for item in plan["dependencyClosure"]
        ),
        "frozenSet": "；".join(plan["frozenSet"]),
        "mediumContract": "；".join(f"{key}: {value}" for key, value in source_analysis["visualContract"].items()),
        "residueCleanup": "清理旧身份特征、旧轮廓、水印、签名、平台标和账户标。",
        "spatialRelations": "；".join(source_analysis["spatialRelations"]),
        "output": "保持完整画布与原比例，清晰输出，不新增文字。",
    }
    identity_contract = rules["identityReplacementContract"]
    plan_fields = identity_contract["planFields"]
    if plan_fields["route"] in plan:
        section_roles = identity_contract["generationSectionRoles"]
        route_evidence = plan[plan_fields["route"]]
        card = target.get(identity_contract["candidateFields"]["card"])
        route_parts = [
            f"mode: {route_evidence[identity_contract['routeEvidenceFields']['mode']]}",
            "完整重绘人物与全部身份依赖",
        ]
        if isinstance(card, dict):
            card_fields = identity_contract["candidateCardFields"]
            route_parts.extend(
                [
                    "身份锚点: " + "、".join(card[card_fields["anchors"]]),
                    "反锚点: " + "、".join(card[card_fields["antiAnchors"]]),
                    "玩法融合: " + "、".join(card[card_fields["playFusion"]]),
                ]
            )
        sections[section_roles["route"]] = "；".join(route_parts)
        decision_fields = identity_contract["identityTextDecisionFields"]
        sections[section_roles["identityText"]] = "；".join(
            f"{item[decision_fields['sourceText']]} -> "
            f"{item[decision_fields['action']]} -> {item[decision_fields['result']]}"
            for item in plan[plan_fields["textDecisions"]]
        )
    request_id = "gen-" + _sha_bytes(_canonical_bytes({"plan": plan, "sections": sections}))[:24]
    multi_contract = rules["multiInstanceContract"]
    return {
        "artifactType": "generation-package",
        "schemaVersion": plan["schemaVersion"],
        "requestId": request_id,
        "sections": sections,
        multi_contract["generationFields"]["imageOperations"]: copy.deepcopy(
            plan[multi_contract["planFields"]["imageOperations"]]
        ),
        "output": {"imageCount": 1, "size": source_analysis.get("imageSize", "1024x1024")},
        "replacementPlanSha256": _sha_bytes(_json_bytes(plan)),
    }


def _compile_redo_generation_package(
    previous_package: dict[str, Any],
    previous_review: dict[str, Any],
    revision: int,
) -> dict[str, Any]:
    package = copy.deepcopy(previous_package)
    correction = {
        "revision": revision,
        "failedGates": previous_review.get("decisionEvidence", {}).get("failedGates", []),
        "previousGenerationPackageSha256": _sha_bytes(_canonical_bytes(previous_package)),
        "previousVisualReviewSha256": _sha_bytes(_canonical_bytes(previous_review)),
    }
    package["redo"] = correction
    package["requestId"] = "gen-" + _sha_bytes(
        _canonical_bytes({"previousRequestId": previous_package["requestId"], "correction": correction})
    )[:24]
    return package


def _compile_generation_task(
    generation_package: dict[str, Any],
    source_sha256: str,
    production_pin_sha256: str,
    revision: int,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> dict[str, Any]:
    contract = rules["generationExecutionContract"]
    task_fields = contract["taskFields"]
    intent_fields = contract["requestIntentFields"]
    option_fields = contract["requestOptionFields"]
    generation_package_sha = _sha_bytes(_json_bytes(generation_package))
    request_intent = {
        intent_fields["generationRequestIdentity"]: generation_package["requestId"],
        intent_fields["prompt"]: "\n".join(generation_package["sections"].values()),
        intent_fields["imageCount"]: generation_options[option_fields["imageCount"]],
        intent_fields["primaryOutputIndex"]: generation_options[
            option_fields["primaryOutputIndex"]
        ],
        intent_fields["imageSize"]: generation_package["output"]["size"],
        intent_fields["outputFormat"]: contract["outputFormats"]["png"],
    }
    request_intent_sha = _sha_bytes(_canonical_bytes(request_intent))
    input_sha = _sha_bytes(
        _canonical_bytes(
            {
                task_fields["sourceImageSha256"]: source_sha256,
                task_fields["generationPackageSha256"]: generation_package_sha,
                task_fields["productionPinSha256"]: production_pin_sha256,
                task_fields["requestIntentSha256"]: request_intent_sha,
            }
        )
    )
    identity_payload = {
        task_fields["revision"]: revision,
        task_fields["inputSha256"]: input_sha,
        task_fields["requestIntentSha256"]: request_intent_sha,
    }
    task_id = (
        contract["artifactTypes"]["task"]
        + "-"
        + _sha_bytes(_canonical_bytes(identity_payload))[:24]
    )
    return {
        task_fields["artifactType"]: contract["artifactTypes"]["task"],
        task_fields["schemaVersion"]: rules["schemaVersion"],
        task_fields["taskIdentity"]: task_id,
        task_fields["revision"]: revision,
        task_fields["sourceImageSha256"]: source_sha256,
        task_fields["generationPackageSha256"]: generation_package_sha,
        task_fields["productionPinSha256"]: production_pin_sha256,
        task_fields["inputSha256"]: input_sha,
        task_fields["requestIntent"]: request_intent,
        task_fields["requestIntentSha256"]: request_intent_sha,
    }


def _prepared_generation_wal(
    generation_task: dict[str, Any], timestamp: str, rules: dict[str, Any]
) -> dict[str, Any]:
    contract = rules["generationExecutionContract"]
    task_fields = contract["taskFields"]
    wal_fields = contract["walFields"]
    return {
        wal_fields["artifactType"]: contract["artifactTypes"]["wal"],
        wal_fields["schemaVersion"]: rules["schemaVersion"],
        wal_fields["taskIdentity"]: generation_task[task_fields["taskIdentity"]],
        wal_fields["taskSha256"]: _sha_bytes(_json_bytes(generation_task)),
        wal_fields["previousWalSha256"]: None,
        wal_fields["revision"]: generation_task[task_fields["revision"]],
        wal_fields["status"]: contract["walStatuses"]["prepared"],
        wal_fields["provider"]: None,
        wal_fields["model"]: None,
        wal_fields["providerRequestIdentity"]: None,
        wal_fields["providerOutputIdentity"]: None,
        wal_fields["outputSha256"]: None,
        wal_fields["outputAssets"]: [],
        wal_fields["pollAttemptCount"]: 0,
        wal_fields["failureClass"]: None,
        wal_fields["failureReason"]: None,
        wal_fields["updatedAt"]: timestamp,
    }


def _generation_failure_stop(
    failure_class: str,
    failure_reason: str,
    rules: dict[str, Any],
    evidence: dict[str, Any],
) -> WorkflowStop:
    contract = rules["generationExecutionContract"]
    failure_reason = _sanitize_generation_failure_reason(failure_reason, rules)
    failure_role = next(
        (
            role
            for role, value in contract["failureClasses"].items()
            if value == failure_class
        ),
        "permanent",
    )
    route = contract["failureRoutes"][failure_role]
    recovery_phase_index = route["recoveryPhaseIndex"]
    routed_evidence = {
        **evidence,
        "failureClass": failure_class,
        "failureReason": failure_reason,
        "recoverablePhase": (
            rules["productionPhases"][recovery_phase_index]["phase"]
            if recovery_phase_index is not None
            else None
        ),
    }
    return _stop(
        rules,
        route["outcomeRole"],
        route["errorCodeRole"],
        "生成任务未完成，已按 failure class 路由到稳定恢复阶段。",
        routed_evidence,
    )


def _sanitize_generation_failure_reason(value: Any, rules: dict[str, Any]) -> str:
    contract = rules["generationExecutionContract"]["persistedErrorSanitization"]
    detail = value if isinstance(value, str) else type(value).__name__
    return contract["digestPrefix"] + _sha_bytes(detail.encode("utf-8"))[
        : contract["digestLength"]
    ]


def _generation_submission_shape_valid(
    submission: dict[str, Any], rules: dict[str, Any]
) -> bool:
    contract = rules["generationExecutionContract"]
    fields = contract["submissionFields"]
    if set(submission) != set(fields.values()):
        return False
    status = submission.get(fields["status"])
    if status == contract["submissionStatuses"]["submitted"]:
        return bool(
            _execution_identity_valid(
                submission.get(fields["provider"]),
                contract["providerIdentityPattern"],
            )
            and _execution_identity_valid(
                submission.get(fields["model"]), contract["modelIdentityPattern"]
            )
            and _execution_identity_valid(
                submission.get(fields["providerRequestIdentity"]),
                contract["opaqueExecutionIdentityPattern"],
            )
            and submission.get(fields["failureClass"]) is None
            and submission.get(fields["failureReason"]) is None
        )
    if status == contract["submissionStatuses"]["failed"]:
        return bool(
            _execution_identity_valid(
                submission.get(fields["provider"]),
                contract["providerIdentityPattern"],
            )
            and _execution_identity_valid(
                submission.get(fields["model"]), contract["modelIdentityPattern"]
            )
            and submission.get(fields["providerRequestIdentity"]) is None
            and submission.get(fields["failureClass"])
            in contract["failureClasses"].values()
            and isinstance(submission.get(fields["failureReason"]), str)
            and submission[fields["failureReason"]].strip()
        )
    return False


def _execution_identity_valid(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _generation_output_assets_valid(
    output_assets: Any,
    expected_count: int,
    asset_fields: dict[str, str],
    output_identity_pattern: str,
) -> bool:
    if not isinstance(output_assets, list) or len(output_assets) != expected_count:
        return False
    output_identities: list[str] = []
    for asset in output_assets:
        if not (
            isinstance(asset, dict)
            and set(asset) == set(asset_fields.values())
            and _execution_identity_valid(
                asset.get(asset_fields["providerOutputIdentity"]),
                output_identity_pattern,
            )
            and isinstance(asset.get(asset_fields["sha256"]), str)
            and re.fullmatch(r"[0-9a-f]{64}", asset[asset_fields["sha256"]])
        ):
            return False
        output_identities.append(asset[asset_fields["providerOutputIdentity"]])
    return len(output_identities) == len(set(output_identities))


def _image_bytes_match_output_format(
    payload: Any, output_format: str, contract: dict[str, Any]
) -> bool:
    if not isinstance(payload, bytes) or not payload:
        return False
    format_role = next(
        (role for role, value in contract["outputFormats"].items() if value == output_format),
        None,
    )
    if format_role is None or not payload.startswith(
        bytes.fromhex(contract["outputFormatSignatures"][format_role])
    ):
        return False
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if (
                image.format != contract["outputFormatDecoderNames"][format_role]
                or getattr(image, "n_frames", 1) != 1
            ):
                return False
            width, height = image.size
            image.verify()
        with Image.open(io.BytesIO(payload)) as decoded:
            decoded.load()
        return bool(
            1 <= width <= contract["maximumDecodedImageDimension"]
            and 1 <= height <= contract["maximumDecodedImageDimension"]
            and width * height <= contract["maximumDecodedImagePixels"]
        )
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError):
        return False


def image_bytes_match_output_format(
    payload: Any, output_format: str, contract: dict[str, Any]
) -> bool:
    """Validate a generated single-image payload against the shared machine contract."""

    return _image_bytes_match_output_format(payload, output_format, contract)


def _generation_poll_shape_valid(
    poll_result: dict[str, Any], generation_task: dict[str, Any], rules: dict[str, Any]
) -> bool:
    contract = rules["generationExecutionContract"]
    fields = contract["pollResultFields"]
    if set(poll_result) != set(fields.values()):
        return False
    status = poll_result.get(fields["status"])
    if status == contract["pollStatuses"]["failed"]:
        return bool(
            poll_result.get(fields["failureClass"])
            in contract["failureClasses"].values()
            and isinstance(poll_result.get(fields["failureReason"]), str)
            and poll_result[fields["failureReason"]].strip()
            and poll_result.get(fields["extension"]) is None
            and poll_result.get(fields["imageBytes"]) is None
            and poll_result.get(fields["providerOutputIdentity"]) is None
            and poll_result.get(fields["outputAssets"]) == []
        )
    if status != contract["pollStatuses"]["succeeded"]:
        return False
    task_fields = contract["taskFields"]
    intent_fields = contract["requestIntentFields"]
    asset_fields = contract["outputAssetFields"]
    output_assets = poll_result.get(fields["outputAssets"])
    image_bytes = poll_result.get(fields["imageBytes"])
    image_count = generation_task[task_fields["requestIntent"]][
        intent_fields["imageCount"]
    ]
    primary_index = generation_task[task_fields["requestIntent"]][
        intent_fields["primaryOutputIndex"]
    ]
    output_format = generation_task[task_fields["requestIntent"]][
        intent_fields["outputFormat"]
    ]
    return bool(
        poll_result.get(fields["failureClass"]) is None
        and poll_result.get(fields["failureReason"]) is None
        and isinstance(poll_result.get(fields["extension"]), str)
        and poll_result[fields["extension"]]
        == contract["outputFormatExtensions"][
            next(
                role
                for role, value in contract["outputFormats"].items()
                if value
                == generation_task[task_fields["requestIntent"]][
                    intent_fields["outputFormat"]
                ]
            )
        ]
        and isinstance(image_bytes, bytes)
        and image_bytes
        and _image_bytes_match_output_format(image_bytes, output_format, contract)
        and _execution_identity_valid(
            poll_result.get(fields["providerOutputIdentity"]),
            contract["opaqueExecutionIdentityPattern"],
        )
        and _generation_output_assets_valid(
            output_assets,
            image_count,
            asset_fields,
            contract["opaqueExecutionIdentityPattern"],
        )
        and output_assets[primary_index][asset_fields["providerOutputIdentity"]]
        == poll_result[fields["providerOutputIdentity"]]
        and output_assets[primary_index][asset_fields["sha256"]]
        == _sha_bytes(image_bytes)
    )


def _generation_task_wal_errors(
    generation_task: Any,
    wal: Any,
    generation_package: dict[str, Any],
    source_sha256: str,
    production_pin_sha256: str,
    revision: int,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> list[str]:
    contract = rules["generationExecutionContract"]
    task_fields = contract["taskFields"]
    wal_fields = contract["walFields"]
    errors: list[str] = []
    if not isinstance(generation_task, dict) or set(generation_task) != set(
        task_fields.values()
    ):
        return ["generation task shape invalid"]
    expected_task = _compile_generation_task(
        generation_package,
        source_sha256,
        production_pin_sha256,
        revision,
        generation_options,
        rules,
    )
    if generation_task != expected_task:
        return ["generation task identity mismatch"]
    if not isinstance(wal, dict) or set(wal) != set(wal_fields.values()):
        errors.append("generation WAL shape invalid")
        return errors
    if wal.get(wal_fields["taskIdentity"]) != generation_task[task_fields["taskIdentity"]]:
        errors.append("generation WAL task identity mismatch")
    if wal.get(wal_fields["taskSha256"]) != _sha_bytes(_json_bytes(generation_task)):
        errors.append("generation WAL task digest mismatch")
    if wal.get(wal_fields["revision"]) != revision:
        errors.append("generation WAL revision mismatch")
    if wal.get(wal_fields["status"]) not in contract["walStatuses"].values():
        errors.append("generation WAL status invalid")
    poll_attempt_count = wal.get(wal_fields["pollAttemptCount"])
    if (
        not isinstance(poll_attempt_count, int)
        or isinstance(poll_attempt_count, bool)
        or poll_attempt_count < 0
    ):
        errors.append("generation WAL poll attempt count invalid")
    if not isinstance(wal.get(wal_fields["updatedAt"]), str) or not wal[
        wal_fields["updatedAt"]
    ].strip():
        errors.append("generation WAL timestamp invalid")
    status = wal.get(wal_fields["status"])
    previous_wal_sha = wal.get(wal_fields["previousWalSha256"])
    provider_values = [
        wal.get(wal_fields[role])
        for role in ("provider", "model", "providerRequestIdentity")
    ]
    output_assets = wal.get(wal_fields["outputAssets"])
    retry_budget = contract["retryBudgets"]["retryable"]
    if status == contract["walStatuses"]["prepared"]:
        if previous_wal_sha is not None:
            errors.append("prepared generation WAL has previous digest")
        if any(value is not None for value in provider_values):
            errors.append("prepared generation WAL has provider credentials")
        if (
            wal.get(wal_fields["providerOutputIdentity"]) is not None
            or wal.get(wal_fields["outputSha256"]) is not None
            or output_assets != []
            or wal.get(wal_fields["failureClass"]) is not None
            or wal.get(wal_fields["failureReason"]) is not None
        ):
            errors.append("prepared generation WAL has terminal evidence")
        if poll_attempt_count != 0:
            errors.append("prepared generation WAL poll count invalid")
    elif status in {
        contract["walStatuses"]["submitted"],
        contract["walStatuses"]["succeeded"],
    }:
        if not isinstance(previous_wal_sha, str) or re.fullmatch(
            r"[0-9a-f]{64}", previous_wal_sha
        ) is None:
            errors.append("generation WAL previous digest invalid")
        if not (
            _execution_identity_valid(
                provider_values[0], contract["providerIdentityPattern"]
            )
            and _execution_identity_valid(
                provider_values[1], contract["modelIdentityPattern"]
            )
            and _execution_identity_valid(
                provider_values[2], contract["opaqueExecutionIdentityPattern"]
            )
        ):
            errors.append("submitted generation WAL provider credentials invalid")
        if (
            wal.get(wal_fields["failureClass"]) is not None
            or wal.get(wal_fields["failureReason"]) is not None
        ):
            errors.append("active generation WAL has failure evidence")
        if status == contract["walStatuses"]["submitted"] and not (
            isinstance(poll_attempt_count, int) and 0 <= poll_attempt_count <= retry_budget
        ):
            errors.append("submitted generation WAL poll count invalid")
        if status == contract["walStatuses"]["succeeded"] and not (
            isinstance(poll_attempt_count, int) and 1 <= poll_attempt_count <= retry_budget
        ):
            errors.append("succeeded generation WAL poll count invalid")
    elif status == contract["walStatuses"]["failed"]:
        if not isinstance(previous_wal_sha, str) or re.fullmatch(
            r"[0-9a-f]{64}", previous_wal_sha
        ) is None:
            errors.append("generation WAL previous digest invalid")
        failure_class = wal.get(wal_fields["failureClass"])
        provider, model, provider_request_id = provider_values
        provider_model_valid = bool(
            (provider is None and model is None)
            or (
                _execution_identity_valid(provider, contract["providerIdentityPattern"])
                and _execution_identity_valid(model, contract["modelIdentityPattern"])
            )
        )
        if failure_class not in contract["failureClasses"].values():
            errors.append("failed generation WAL failure class invalid")
        if not isinstance(wal.get(wal_fields["failureReason"]), str) or not wal[
            wal_fields["failureReason"]
        ].strip():
            errors.append("failed generation WAL reason invalid")
        if not provider_model_valid or (
            provider_request_id is not None
            and (
                not _execution_identity_valid(
                    provider_request_id, contract["opaqueExecutionIdentityPattern"]
                )
                or provider is None
                or model is None
            )
        ):
            errors.append("failed generation WAL provider evidence invalid")
        if (
            failure_class == contract["failureClasses"]["retryable"]
            and not (
                provider_model_valid
                and _execution_identity_valid(
                    provider_request_id, contract["opaqueExecutionIdentityPattern"]
                )
            )
        ):
            errors.append("retryable generation WAL request identity missing")
        if (
            failure_class == contract["failureClasses"]["submissionUnknown"]
            and provider_request_id is not None
        ):
            errors.append("unknown submission WAL cannot claim a request identity")
        if failure_class == contract["failureClasses"]["submissionUnknown"]:
            if poll_attempt_count != 0:
                errors.append("unknown submission WAL poll count invalid")
        elif failure_class == contract["failureClasses"]["retryable"]:
            if not (
                isinstance(poll_attempt_count, int)
                and 1 <= poll_attempt_count < retry_budget
            ):
                errors.append("retryable generation WAL poll count invalid")
        elif not (
            isinstance(poll_attempt_count, int) and 0 <= poll_attempt_count <= retry_budget
        ):
            errors.append("failed generation WAL poll count invalid")
    if status != contract["walStatuses"]["succeeded"]:
        if (
            wal.get(wal_fields["providerOutputIdentity"]) is not None
            or wal.get(wal_fields["outputSha256"]) is not None
            or output_assets != []
        ):
            errors.append("unfinished generation WAL has output evidence")
    else:
        intent_fields = contract["requestIntentFields"]
        asset_fields = contract["outputAssetFields"]
        intent = generation_task[task_fields["requestIntent"]]
        if (
            not _generation_output_assets_valid(
                output_assets,
                intent[intent_fields["imageCount"]],
                asset_fields,
                contract["opaqueExecutionIdentityPattern"],
            )
        ):
            errors.append("succeeded generation WAL output assets invalid")
        if not _execution_identity_valid(
            wal.get(wal_fields["providerOutputIdentity"]),
            contract["opaqueExecutionIdentityPattern"],
        ):
            errors.append("succeeded generation WAL output identity invalid")
        if not isinstance(wal.get(wal_fields["outputSha256"]), str) or re.fullmatch(
            r"[0-9a-f]{64}", wal[wal_fields["outputSha256"]]
        ) is None:
            errors.append("succeeded generation WAL output digest invalid")
        if isinstance(output_assets, list) and len(output_assets) == intent[
            intent_fields["imageCount"]
        ]:
            primary_asset = output_assets[intent[intent_fields["primaryOutputIndex"]]]
            if isinstance(primary_asset, dict) and (
                primary_asset.get(asset_fields["providerOutputIdentity"])
                != wal.get(wal_fields["providerOutputIdentity"])
                or primary_asset.get(asset_fields["sha256"])
                != wal.get(wal_fields["outputSha256"])
            ):
                errors.append("generation WAL primary output mismatch")
    return errors


def _load_generation_execution_evidence(
    output_dir: Path,
    package_name: str,
    task_name: str,
    wal_name: str,
    source_sha256: str,
    revision: int,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> tuple[list[str], Any, Any, Any]:
    try:
        generation_package = _load_json(output_dir / package_name)
        generation_task = _load_json(output_dir / task_name)
        generation_wal = _load_json(output_dir / wal_name)
        errors = _generation_task_wal_errors(
            generation_task,
            generation_wal,
            generation_package,
            source_sha256,
            _sha_file(output_dir / "production-pin.json"),
            revision,
            generation_options,
            rules,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["generation execution evidence unreadable"], None, None, None
    return errors, generation_package, generation_task, generation_wal


def _adopt_pre_submit_generation_staging(
    output_dir: Path,
    manifest: dict[str, Any],
    source_sha256: str,
    generation_options: dict[str, int],
    rules: dict[str, Any],
    timestamp: str,
    phase: str,
) -> tuple[list[str], Any, Any, Any]:
    """Validate and register an interrupted, provider-side-effect-free P2 staging set."""
    revision = manifest.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return ["generation staging revision invalid"], None, None, None
    package_name = _revisioned_name("generation-package.json", revision)
    task_name = _revisioned_name("generation-task.json", revision)
    wal_name = _revisioned_name("generation-wal.json", revision)
    package_path = output_dir / package_name
    task_path = output_dir / task_name
    wal_path = output_dir / wal_name
    try:
        source_analysis = _load_json(output_dir / "source-analysis.json")
        plan = _load_json(output_dir / "replacement-plan.json")
        expected_package = _compile_generation_package(plan, source_analysis, rules)
        contract = rules["generationExecutionContract"]
        expected_package["output"]["imageCount"] = generation_options[
            contract["requestOptionFields"]["imageCount"]
        ]
        if package_path.is_file():
            generation_package = _load_json(package_path)
            if generation_package != expected_package:
                return ["untracked generation package mismatch"], None, None, None
        else:
            generation_package = expected_package
            _atomic_write_new(package_path, _json_bytes(generation_package))
        expected_task = _compile_generation_task(
            generation_package,
            source_sha256,
            _sha_file(output_dir / "production-pin.json"),
            revision,
            generation_options,
            rules,
        )
        if task_path.is_file():
            generation_task = _load_json(task_path)
            if generation_task != expected_task:
                return ["untracked generation task mismatch"], None, None, None
        else:
            generation_task = expected_task
            _atomic_write_new(task_path, _json_bytes(generation_task))
        if wal_path.is_file():
            generation_wal = _load_json(wal_path)
        else:
            generation_wal = _prepared_generation_wal(
                generation_task, timestamp, rules
            )
            _write_generation_wal(wal_path, generation_wal, rules)
        errors = _generation_task_wal_errors(
            generation_task,
            generation_wal,
            generation_package,
            source_sha256,
            _sha_file(output_dir / "production-pin.json"),
            revision,
            generation_options,
            rules,
        )
        wal_fields = contract["walFields"]
        if generation_wal.get(wal_fields["status"]) != contract["walStatuses"][
            "prepared"
        ]:
            errors.append("untracked generation WAL is not prepared")
        if errors:
            return errors, generation_package, generation_task, generation_wal
        _record_artifact(
            manifest, output_dir, package_name, phase, ["replacement-plan.json"]
        )
        _record_artifact(
            manifest,
            output_dir,
            task_name,
            phase,
            [package_name, "production-pin.json"],
        )
        _record_artifact(manifest, output_dir, wal_name, phase, [task_name])
        _persist_manifest(output_dir, manifest)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return ["generation staging evidence unreadable"], None, None, None
    return [], generation_package, generation_task, generation_wal


def _current_generation_execution_errors(
    output_dir: Path,
    manifest: dict[str, Any],
    source_sha256: str,
    generation_options: dict[str, int],
    rules: dict[str, Any],
) -> list[str]:
    revision = manifest.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return ["generation execution revision invalid"]
    package_name = _revisioned_name("generation-package.json", revision)
    task_name = _revisioned_name("generation-task.json", revision)
    wal_name = _revisioned_name("generation-wal.json", revision)
    candidate_names = _revision_image_artifacts(
        manifest, "generated-candidate-image", revision
    )
    if len(candidate_names) != 1:
        return ["current generated candidate count must be one"]
    required_paths = {
        package_name: output_dir / package_name,
        task_name: output_dir / task_name,
        wal_name: output_dir / wal_name,
        "production-pin.json": output_dir / "production-pin.json",
        candidate_names[0]: output_dir / candidate_names[0],
    }
    missing = [name for name, path in required_paths.items() if not path.is_file()]
    if missing:
        return [f"generation execution artifact missing: {name}" for name in missing]
    errors, generation_package, generation_task, generation_wal = (
        _load_generation_execution_evidence(
            output_dir,
            package_name,
            task_name,
            wal_name,
            source_sha256,
            revision,
            generation_options,
            rules,
        )
    )
    if errors:
        return errors
    contract = rules["generationExecutionContract"]
    wal_fields = contract["walFields"]
    if generation_wal[wal_fields["status"]] != contract["walStatuses"]["succeeded"]:
        errors.append("current generation WAL is not succeeded")
    candidate_sha = _sha_file(required_paths[candidate_names[0]])
    if generation_wal[wal_fields["outputSha256"]] != candidate_sha:
        errors.append("generation WAL candidate digest mismatch")
    task_fields = contract["taskFields"]
    intent_fields = contract["requestIntentFields"]
    output_format = generation_task[task_fields["requestIntent"]][
        intent_fields["outputFormat"]
    ]
    output_format_role = next(
        role
        for role, value in contract["outputFormats"].items()
        if value == output_format
    )
    candidate_path = required_paths[candidate_names[0]]
    if (
        candidate_path.suffix != contract["outputFormatExtensions"][output_format_role]
        or not _image_bytes_match_output_format(
            candidate_path.read_bytes(), output_format, contract
        )
    ):
        errors.append("generation candidate format mismatch")
    return errors


def _evaluate_visual_gate(
    review: Any,
    rules: dict[str, Any],
    expected_bindings: dict[str, str],
    identity_text_required: bool,
    expected_image_operations: list[dict[str, Any]],
) -> WorkflowStop | None:
    if not isinstance(review, dict):
        return _stop(
            rules,
            "failed",
            "externalFailure",
            "视觉审核证据必须是对象。",
            {"actualType": type(review).__name__},
        )
    contract = rules["visualReviewContract"]
    hard_gate_names = set(contract["hardGateRoles"].values())
    cleanliness_names = set(contract["cleanlinessFindingRoles"].values())
    ambiguity_names = set(contract["ambiguitySignalRoles"].values())
    evidence_fields = contract["evidenceFieldRoles"]
    hard_gates = review.get(evidence_fields["hardGates"])
    dimensions = review.get(evidence_fields["visualDimensions"])
    visible_text = review.get(evidence_fields["visibleText"])
    cleanliness = review.get(evidence_fields["cleanliness"])
    ambiguities = review.get(evidence_fields["ambiguity"])
    identity_text_field = evidence_fields["identityText"]
    identity_text = review.get(identity_text_field)
    identity_text_fields = contract["identityTextEvidenceFields"]
    multi_contract = rules["multiInstanceContract"]
    operation_fields = multi_contract["operationFields"]
    operation_review_fields = multi_contract["operationReviewFields"]
    operation_evidence = review.get(evidence_fields["imageOperations"])
    expected_operation_ids = {
        operation[operation_fields["identity"]] for operation in expected_image_operations
    }
    bindings = review.get("bindings")
    method = review.get("method")
    evidence_payload = (
        {field: review[field] for field in evidence_fields.values()}
        if all(field in review for field in evidence_fields.values())
        else None
    )
    contract_valid = bool(
        review.get("artifactType") == "visual-review"
        and review.get("schemaVersion") == rules["schemaVersion"]
        and isinstance(hard_gates, dict)
        and set(hard_gates) == hard_gate_names
        and all(isinstance(value, bool) for value in hard_gates.values())
        and isinstance(dimensions, dict)
        and set(dimensions) == set(rules["visualDimensions"])
        and all(
            isinstance(value, dict)
            and isinstance(value.get("pass"), bool)
            and isinstance(value.get("evidence"), str)
            and value.get("evidence").strip()
            for value in dimensions.values()
        )
        and isinstance(visible_text, dict)
        and isinstance(visible_text.get("pass"), bool)
        and isinstance(visible_text.get("evidence"), str)
        and visible_text.get("evidence").strip()
        and isinstance(cleanliness, dict)
        and set(cleanliness) == cleanliness_names
        and all(isinstance(value, bool) for value in cleanliness.values())
        and isinstance(ambiguities, dict)
        and set(ambiguities) == ambiguity_names
        and all(isinstance(value, bool) for value in ambiguities.values())
        and isinstance(identity_text, dict)
        and set(identity_text) == set(identity_text_fields.values())
        and identity_text.get(identity_text_fields["applicability"]) is identity_text_required
        and isinstance(identity_text.get(identity_text_fields["legacyTermsAbsent"]), bool)
        and isinstance(identity_text.get(identity_text_fields["replacementConsistency"]), bool)
        and isinstance(identity_text.get(identity_text_fields["explanation"]), str)
        and identity_text[identity_text_fields["explanation"]].strip()
        and isinstance(operation_evidence, list)
        and len(operation_evidence) == len(expected_operation_ids)
        and all(
            isinstance(item, dict)
            and set(item) == set(operation_review_fields.values())
            and isinstance(
                item.get(operation_review_fields["operationIdentity"]), str
            )
            and item.get(operation_review_fields["operationIdentity"])
            in expected_operation_ids
            and all(
                isinstance(item.get(operation_review_fields[field]), bool)
                for field in (
                    "targetCleared",
                    "anchorsStable",
                    "relationsPreserved",
                    "nonTargetStable",
                )
            )
            and isinstance(item.get(operation_review_fields["explanation"]), str)
            and item[operation_review_fields["explanation"]].strip()
            for item in operation_evidence
        )
        and len(
            {
                item[operation_review_fields["operationIdentity"]]
                for item in operation_evidence
            }
        )
        == len(operation_evidence)
        and isinstance(bindings, dict)
        and all(bindings.get(key) == value for key, value in expected_bindings.items())
        and evidence_payload is not None
        and bindings.get("evidenceSha256") == _sha_bytes(_canonical_bytes(evidence_payload))
        and isinstance(method, dict)
        and isinstance(method.get("id"), str)
        and method.get("id").strip()
        and isinstance(method.get("version"), str)
        and method.get("version").strip()
        and isinstance(review.get("reviewedAt"), str)
        and review.get("reviewedAt").strip()
    )
    if not contract_valid:
        review["decision"] = contract["decisionValues"]["rejected"]
        review["decisionEvidence"] = {"contractValid": False}
        return _stop(
            rules,
            "failed",
            "externalFailure",
            "视觉审核证据合同无效或未绑定当前生图事实。",
            {"expectedBindings": expected_bindings},
        )
    failures = [name for name, passed in hard_gates.items() if passed is not True]
    failures.extend(name for name, value in dimensions.items() if value["pass"] is not True)
    failures.extend(name for name, found in cleanliness.items() if found is True)
    if visible_text["pass"] is not True:
        failures.append(contract["hardGateRoles"]["visibleText"])
    if identity_text_required and (
        identity_text[identity_text_fields["legacyTermsAbsent"]] is not True
        or identity_text[identity_text_fields["replacementConsistency"]] is not True
    ):
        failures.append(contract["hardGateRoles"]["visibleText"])
        failures.append(contract["hardGateRoles"]["legacyIdentityAbsence"])
    for item in operation_evidence:
        if item[operation_review_fields["targetCleared"]] is not True:
            failures.append(contract["hardGateRoles"]["dependencyClosure"])
        if item[operation_review_fields["anchorsStable"]] is not True:
            failures.append(contract["hardGateRoles"]["nonTargetPreservation"])
        if item[operation_review_fields["relationsPreserved"]] is not True:
            failures.append(contract["hardGateRoles"]["contactGeometry"])
        if item[operation_review_fields["nonTargetStable"]] is not True:
            failures.append(contract["hardGateRoles"]["nonTargetPreservation"])
    if failures:
        failed_gates = sorted(set(failures))
        review["decision"] = contract["decisionValues"]["rejected"]
        review["decisionEvidence"] = {"failedGates": failed_gates}
        return _stop(
            rules,
            "blocked",
            "visualHardFailure",
            "生成图未通过模板图视觉硬门禁，必须修正或重生成。",
            {"failedGates": failed_gates},
        )
    review_signals = sorted(name for name, present in ambiguities.items() if present is True)
    if review_signals:
        review["decision"] = contract["decisionValues"]["needsReview"]
        review["decisionEvidence"] = {"reviewSignals": review_signals}
        return _stop(
            rules,
            "needs_input",
            "riskNeedsReview",
            "生成图存在歧义、审美风险或证据不足，需要人工复核。",
            {"reviewSignals": review_signals},
        )
    review["decision"] = contract["decisionValues"]["approved"]
    review["decisionEvidence"] = {"hardGatesPassed": True}
    return None


def _text_tokens_follow_source(
    source_text: str, tokens: list[str], common_punctuation: set[str]
) -> bool:
    cursor = 0
    for token in tokens:
        position = source_text.find(token, cursor)
        if position < 0:
            return False
        cursor = position + len(token)
    normalize = lambda value: "".join(
        character
        for character in value
        if not character.isspace() and character not in common_punctuation
    )
    return normalize("".join(tokens)) == normalize(source_text)


def _normalized_visible_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if not character.isspace()
        and not unicodedata.category(character).startswith(("P", "Z"))
    )


def _validate_visible_text_contract(
    analysis: dict[str, Any], slots: list[dict[str, Any]], rules: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    contract = rules["visibleTextContract"]
    analysis_fields = contract["analysisFields"]
    inventory_fields = contract["inventoryFields"]
    region_fields = contract["regionFields"]
    evidence_fields = contract["exactEvidenceFields"]
    actions = contract["actions"]
    roles = set(contract["roles"].values())
    value_classes = set(contract["valueClasses"].values())
    language_values = contract["languageValues"]
    regions = analysis.get(analysis_fields["regions"])
    inventory = analysis.get(analysis_fields["inventory"])

    inventory_valid = bool(
        isinstance(regions, list)
        and isinstance(inventory, dict)
        and set(inventory) == set(inventory_fields.values())
        and inventory.get(inventory_fields["complete"]) is True
        and isinstance(inventory.get(inventory_fields["regionIdentities"]), list)
        and all(
            isinstance(value, str) and value.strip()
            for value in inventory.get(inventory_fields["regionIdentities"], [])
        )
        and len(inventory.get(inventory_fields["regionIdentities"], []))
        == len(set(inventory.get(inventory_fields["regionIdentities"], [])))
        and isinstance(inventory.get(inventory_fields["explanation"]), str)
        and inventory[inventory_fields["explanation"]].strip()
    )
    if not inventory_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见文字清单必须声明完整区域集合并提供非空证据。",
            {},
        )

    region_ids: list[str] = []
    malformed_region_ids: list[str] = []
    invalid_fidelity_ids: list[str] = []
    invalid_route_ids: list[str] = []
    review_region_ids: list[str] = []
    removal_region_ids: list[str] = []
    slot_bindings: dict[str, str] = {}
    common_punctuation = set(contract["commonPunctuationCharacters"])
    for index, region in enumerate(regions):
        fallback_id = f"region-{index}"
        if not isinstance(region, dict):
            malformed_region_ids.append(fallback_id)
            continue
        region_id = region.get(region_fields["identity"])
        source_text = region.get(region_fields["sourceText"])
        role = region.get(region_fields["role"])
        value_class = region.get(region_fields["valueClass"])
        action = region.get(region_fields["action"])
        selected_text = region.get(region_fields["selectedText"])
        evidence = region.get(region_fields["exactTextEvidence"])
        base_region_fields = set(region_fields.values()) - {region_fields["slotIdentity"]}
        expected_region_fields = base_region_fields | (
            {region_fields["slotIdentity"]}
            if action == actions["openSlot"]
            else set()
        )
        if not (
            isinstance(region_id, str)
            and region_id.strip()
            and isinstance(source_text, str)
            and source_text.strip()
            and isinstance(role, str)
            and role in roles
            and isinstance(value_class, str)
            and value_class in value_classes
            and isinstance(action, str)
            and action in set(contract["actions"].values())
            and isinstance(selected_text, str)
            and isinstance(evidence, dict)
            and set(evidence) == set(evidence_fields.values())
            and set(region) == expected_region_fields
        ):
            malformed_region_ids.append(region_id if isinstance(region_id, str) else fallback_id)
            continue
        region_ids.append(region_id)

        language = evidence.get(evidence_fields["language"])
        tokens = evidence.get(evidence_fields["tokens"])
        lines = evidence.get(evidence_fields["lines"])
        case_tokens = evidence.get(evidence_fields["caseSensitiveTokens"])
        rare_symbols = evidence.get(evidence_fields["rareSymbols"])
        source_has_cjk = bool(CJK_CHARACTER.search(source_text))
        source_has_latin = bool(re.search(r"[A-Za-z]", source_text))
        source_has_kana = bool(re.search(contract["japaneseKanaPattern"], source_text))
        source_has_hangul = bool(re.search(contract["koreanHangulPattern"], source_text))
        if source_has_latin and (source_has_cjk or source_has_kana or source_has_hangul):
            allowed_languages = {language_values["mixed"]}
        elif source_has_kana:
            allowed_languages = {language_values["japanese"]}
        elif source_has_hangul:
            allowed_languages = {language_values["korean"]}
        elif source_has_cjk:
            allowed_languages = {
                language_values["simplifiedChinese"],
                language_values["traditionalChinese"],
            }
        elif source_has_latin:
            allowed_languages = {language_values["english"]}
        else:
            allowed_languages = {language_values["undetermined"]}
        expected_case_tokens = {
            token for token in (tokens if isinstance(tokens, list) else [])
            if isinstance(token, str) and re.search(r"[A-Za-z]", token)
        }
        expected_rare_symbols = {
            character
            for character in source_text
            if not character.isalnum()
            and not character.isspace()
            and character not in common_punctuation
        }
        fidelity_valid = bool(
            language in allowed_languages
            and isinstance(tokens, list)
            and tokens
            and all(isinstance(value, str) and value for value in tokens)
            and _text_tokens_follow_source(source_text, tokens, common_punctuation)
            and isinstance(lines, list)
            and lines == source_text.splitlines()
            and isinstance(case_tokens, list)
            and all(isinstance(value, str) and value in source_text for value in case_tokens)
            and len(case_tokens) == len(set(case_tokens))
            and set(case_tokens) == expected_case_tokens
            and isinstance(rare_symbols, list)
            and all(isinstance(value, str) and len(value) == 1 for value in rare_symbols)
            and len(rare_symbols) == len(set(rare_symbols))
            and set(rare_symbols) == expected_rare_symbols
            and isinstance(evidence.get(evidence_fields["symbolTopology"]), str)
            and evidence[evidence_fields["symbolTopology"]].strip()
            and isinstance(evidence.get(evidence_fields["explanation"]), str)
            and evidence[evidence_fields["explanation"]].strip()
        )
        if not fidelity_valid:
            invalid_fidelity_ids.append(region_id)

        if action not in contract["allowedActionsByRole"].get(role, []):
            invalid_route_ids.append(region_id)
        elif action == actions["remove"]:
            removal_region_ids.append(region_id)
        if action == actions["review"]:
            review_region_ids.append(region_id)
        if action == actions["openSlot"]:
            slot_id = region.get(region_fields["slotIdentity"])
            if (
                value_class not in set(contract["openSlotValueClasses"])
                or not isinstance(slot_id, str)
                or not slot_id.strip()
                or not selected_text
                or selected_text not in source_text
                or (
                    value_class == contract["valueClasses"]["highValueSpan"]
                    and selected_text == source_text
                )
                or (
                    selected_text == source_text
                    and len(source_text) > contract["wholeRegionSlotHardMaximum"]
                )
            ):
                invalid_route_ids.append(region_id)
            elif slot_id in slot_bindings:
                invalid_route_ids.append(region_id)
            else:
                slot_bindings[slot_id] = region_id
        elif action == actions["freeEditable"]:
            if (
                value_class not in set(contract["freeEditableValueClasses"])
                or not selected_text
                or selected_text != source_text
            ):
                invalid_route_ids.append(region_id)
        elif action == actions["preserve"] and selected_text != source_text:
            invalid_route_ids.append(region_id)
        elif action == actions["remove"] and selected_text:
            invalid_route_ids.append(region_id)
        elif action == actions["review"] and selected_text != source_text:
            invalid_route_ids.append(region_id)
        elif value_class == contract["valueClasses"]["secondaryReadable"]:
            invalid_route_ids.append(region_id)
        elif value_class in set(contract["nonSlotValueClasses"]) and action not in {
            actions["preserve"],
            actions["remove"],
            actions["review"],
        }:
            invalid_route_ids.append(region_id)
        if action != actions["openSlot"] and region_fields["slotIdentity"] in region:
            invalid_route_ids.append(region_id)

    expected_region_ids = inventory[inventory_fields["regionIdentities"]]
    if (
        malformed_region_ids
        or len(region_ids) != len(set(region_ids))
        or set(region_ids) != set(expected_region_ids)
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见文字区域未被唯一、完整地分类。",
            {"malformedRegionIds": malformed_region_ids},
        )
    if invalid_fidelity_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见文字的原语种、token、换行、大小写或符号拓扑证据与模板图文字不一致。",
            {inventory_fields["regionIdentities"]: sorted(set(invalid_fidelity_ids))},
        )
    if removal_region_ids:
        raise _stop(
            rules,
            "blocked",
            "visualHardFailure",
            "Approved Template Image 仍含需要清理的可见文字，必须修正模板图后重新分析。",
            {inventory_fields["regionIdentities"]: sorted(set(removal_region_ids))},
        )
    if review_region_ids:
        raise _stop(
            rules,
            "needs_input",
            "riskNeedsReview",
            "可见文字角色或处理方式存在歧义，需要人工复核。",
            {inventory_fields["regionIdentities"]: sorted(set(review_region_ids))},
        )
    if invalid_route_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "文字角色、价值类别与 preserve/remove/open/free-editable 操作不兼容。",
            {inventory_fields["regionIdentities"]: sorted(set(invalid_route_ids))},
        )

    text_slot_type = rules["slotCompilationContract"]["slotTypes"]["visibleTextPrompt"]
    binding_field = contract["slotBindingField"]
    text_slots = [slot for slot in slots if slot.get("type") == text_slot_type]
    binding_valid = bool(
        len(text_slots) == len(slot_bindings)
        and all(
            isinstance(slot.get(binding_field), str)
            and slot_bindings.get(slot["id"]) == slot[binding_field]
            and slot.get("defaultValue")
            == next(
                region[region_fields["selectedText"]]
                for region in regions
                if region[region_fields["identity"]] == slot[binding_field]
            )
            for slot in text_slots
        )
    )
    if not binding_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "文字槽必须与一个高价值文字区域和实际选中文字双向绑定。",
            {},
        )
    over_capacity_text_slots = sorted(
        slot["id"]
        for slot in text_slots
        if any(
            not isinstance(value, str)
            or len(value.strip()) > contract["wholeRegionSlotHardMaximum"]
            for value in slot.get("suggestions", [])
        )
    )
    if over_capacity_text_slots:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "文字槽推荐项超出可稳定排版的短文字容量。",
            {"slotIds": over_capacity_text_slots},
        )
    subject_role = rules["slotCompilationContract"]["semanticRoles"]["primarySubject"]
    subject_open = any(slot.get("semanticRole") == subject_role for slot in slots)
    identity_value_class = contract["valueClasses"]["identityRelated"]

    prompt_template = analysis.get("promptTemplate")
    free_editable = analysis.get("freeEditableContent")
    invalid_free_editable_ids = sorted(
        region[region_fields["identity"]]
        for region in regions
        if region[region_fields["action"]] == actions["freeEditable"]
        and (
            not isinstance(prompt_template, str)
            or region[region_fields["selectedText"]] not in prompt_template
            or not isinstance(free_editable, list)
            or region[region_fields["selectedText"]] not in free_editable
        )
    )
    if invalid_free_editable_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "次要可读文字必须同时进入 Prompt Template 与自由编辑内容。",
            {inventory_fields["regionIdentities"]: invalid_free_editable_ids},
        )
    slot_user_values = [
        value
        for slot in slots
        for value in [slot.get("defaultValue"), *slot.get("suggestions", [])]
        if isinstance(value, str)
    ]
    user_editable_texts = [
        prompt_template if isinstance(prompt_template, str) else "",
        *(free_editable if isinstance(free_editable, list) else []),
        *slot_user_values,
    ]
    normalized_user_editable_texts = [
        _normalized_visible_text(value)
        for value in user_editable_texts
        if isinstance(value, str)
    ]

    def forbidden_region_fragments(region: dict[str, Any]) -> tuple[str, set[str]]:
        evidence = region[region_fields["exactTextEvidence"]]
        source = _normalized_visible_text(region[region_fields["sourceText"]])
        lexical_spans = VISIBLE_TEXT_LEXEME.findall(region[region_fields["sourceText"]])
        tokens = {
            normalized
            for value in [*evidence[evidence_fields["tokens"]], *lexical_spans]
            if isinstance(value, str)
            for normalized in [_normalized_visible_text(value)]
            if len(normalized) >= 2
        }
        return source, tokens

    def fixed_region_reenters_user_content(region: dict[str, Any]) -> bool:
        source, tokens = forbidden_region_fragments(region)
        return bool(
            (source and any(source in value for value in normalized_user_editable_texts))
            or any(token == value for token in tokens for value in normalized_user_editable_texts)
        )

    forbidden_user_text_region_ids = sorted(
        region[region_fields["identity"]]
        for region in regions
        if region[region_fields["action"]] not in {
            actions["openSlot"],
            actions["freeEditable"],
        }
        and not (
            region[region_fields["valueClass"]] == identity_value_class
            and not subject_open
        )
        and fixed_region_reenters_user_content(region)
    )
    if forbidden_user_text_region_ids:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "固定、归因、品牌或清理文字不能重新进入用户 Prompt、自由编辑内容或普通槽位。",
            {inventory_fields["regionIdentities"]: forbidden_user_text_region_ids},
        )
    return copy.deepcopy(regions), copy.deepcopy(inventory)


def _compile_editable_spec(
    analysis: dict[str, Any], rules: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    slot_contract = rules["slotCompilationContract"]
    value_gate_roles = tuple(slot_contract["valueGateRoles"].values())
    allowed_semantic_roles = {
        *slot_contract["semanticRoles"].values(),
        *slot_contract["personAttributeRoles"].values(),
    }
    subject_role = slot_contract["semanticRoles"]["primarySubject"]
    subject_upload_type = slot_contract["slotTypes"]["primarySubjectUpload"]
    slot_candidates = analysis.get("slotCandidates")
    slot_candidates_valid = bool(
        isinstance(slot_candidates, list)
        and all(
            isinstance(slot, dict)
            and isinstance(slot.get("id"), str)
            and SLOT_ID.fullmatch(slot["id"])
            and isinstance(slot.get("semanticRole"), str)
            and slot["semanticRole"] in allowed_semantic_roles
            and slot.get("type") in set(slot_contract["slotTypes"].values())
            and isinstance(slot.get("defaultValue"), str)
            and isinstance(slot.get("suggestions"), list)
            and (slot["type"] == subject_upload_type)
            is (slot["semanticRole"] == subject_role)
            and isinstance(slot.get("valueGates"), dict)
            and set(slot["valueGates"]) == set(value_gate_roles)
            and all(isinstance(slot["valueGates"][role], bool) for role in value_gate_roles)
            for slot in slot_candidates
        )
        and len({slot["id"] for slot in slot_candidates}) == len(slot_candidates)
    )
    if not slot_candidates_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位候选必须提供合法默认值、推荐池和四道具名价值门禁。",
            {},
        )
    slots = [
        copy.deepcopy(slot)
        for slot in slot_candidates
        if all(slot["valueGates"][role] for role in value_gate_roles)
    ]
    identity_contract = rules["identityReplacementContract"]
    identity_plan_fields = identity_contract["planFields"]
    if identity_plan_fields["route"] in plan:
        decision_fields = identity_contract["identityTextDecisionFields"]
        actions = identity_contract["identityTextActions"]
        identity_text_role = slot_contract["semanticRoles"]["identityText"]
        exposed_defaults = {
            decision[decision_fields["result"]]
            for decision in plan[identity_plan_fields["textDecisions"]]
            if decision[decision_fields["action"]] == actions["exposeNeutralSlot"]
        }
        exposed_slots = [
            slot for slot in slots if slot.get("semanticRole") == identity_text_role
        ]
        exposed_slot_defaults = {slot.get("defaultValue") for slot in exposed_slots}
        exposed_slots_are_text = all(
            slot.get("type") == slot_contract["slotTypes"]["visibleTextPrompt"]
            for slot in exposed_slots
        )
        synchronized_text_present = any(
            decision[decision_fields["action"]] == actions["synchronize"]
            for decision in plan[identity_plan_fields["textDecisions"]]
        )
        subject_open = any(
            slot.get("semanticRole") == slot_contract["semanticRoles"]["primarySubject"]
            for slot in slots
        )
        if not exposed_slots_are_text or exposed_defaults != exposed_slot_defaults or (
            synchronized_text_present and subject_open
        ):
            raise _stop(
                rules,
                "blocked",
                "contractFailure",
                "身份文字的中性文字槽与 Replacement Plan 不一致，或具体身份文字与开放主体同时存在。",
                {},
            )
    text_regions, visible_text_inventory = _validate_visible_text_contract(
        analysis, slots, rules
    )
    budget = rules["slotBudget"]
    has_primary_subject = analysis.get("hasPrimarySubject")
    subject_kind = analysis.get("subjectKind")
    person_kind = slot_contract["subjectKinds"]["humanSubject"]
    discriminator_valid = bool(
        isinstance(has_primary_subject, bool)
        and subject_kind in set(slot_contract["subjectKinds"].values())
        and (subject_kind != person_kind or has_primary_subject)
    )
    if not discriminator_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "主体存在性与人物/非人物判别必须提供完整且一致的具名结论。",
            {},
        )
    single_slot_evidence = analysis.get("singleSlotExceptionEvidence")
    single_slot_valid = bool(
        len(slots) == 1
        and isinstance(single_slot_evidence, dict)
        and single_slot_evidence.get("confirmedOnlyOneHighValue") is True
        and isinstance(single_slot_evidence.get("reviewedAxes"), list)
        and all(isinstance(value, str) for value in single_slot_evidence["reviewedAxes"])
        and len(single_slot_evidence["reviewedAxes"])
        == len(set(single_slot_evidence["reviewedAxes"]))
        and set(single_slot_evidence.get("reviewedAxes", []))
        == set(slot_contract["singleSlotReviewAxes"].values())
        and isinstance(single_slot_evidence.get("reason"), str)
        and single_slot_evidence["reason"].strip()
    )
    within_budget = budget["minimum"] <= len(slots) <= budget["maximum"]
    if not within_budget and not single_slot_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "高价值槽位数量不在常态预算内。",
            {"slotCount": len(slots)},
        )
    if has_primary_subject and not any(slot["semanticRole"] == subject_role for slot in slots):
        omission = analysis.get("subjectSlotOmissionEvidence")
        omission_valid = bool(
            isinstance(omission, dict)
            and omission.get("reviewed") is True
            and isinstance(omission.get("valueGates"), dict)
            and set(omission["valueGates"]) == set(value_gate_roles)
            and all(isinstance(value, bool) for value in omission["valueGates"].values())
            and not all(omission["valueGates"].values())
            and isinstance(omission.get("reason"), str)
            and omission["reason"].strip()
        )
        if not omission_valid:
            raise _stop(
                rules,
                "blocked",
                "contractFailure",
                "画面存在明显主体，但高价值槽位没有主体入口或省略证据无效。",
                {},
            )
    if subject_kind == person_kind:
        assessments = analysis.get("subjectAttributeAssessments")
        attribute_roles = set(slot_contract["personAttributeRoles"].values())
        assessment_valid = bool(
            isinstance(assessments, dict)
            and set(assessments) == attribute_roles
            and all(
                isinstance(item, dict)
                and set(item) == {*value_gate_roles, "includedAsSlot", "evidence"}
                and all(isinstance(item.get(gate), bool) for gate in value_gate_roles)
                and isinstance(item.get("includedAsSlot"), bool)
                and isinstance(item.get("evidence"), str)
                and item["evidence"].strip()
                for item in assessments.values()
            )
            and all(
                assessment["includedAsSlot"]
                == all(assessment[gate] for gate in value_gate_roles)
                and assessment["includedAsSlot"]
                == (sum(slot.get("semanticRole") == role for slot in slots) == 1)
                for role, assessment in assessments.items()
            )
        )
        if not assessment_valid:
            raise _stop(
                rules,
                "blocked",
                "contractFailure",
                "人物服装、造型、发型、姿势和颜色缺少独立价值与稳定性评估。",
                {},
            )
    asset_units = analysis.get("assetUnitAnalysis")
    multi_contract = rules["multiInstanceContract"]
    approved_graph = analysis.get(
        multi_contract["approvedFields"]["componentGraph"]
    )
    approved_graph_view = _component_graph_view(approved_graph, rules)
    count_fields = set(slot_contract["assetUnitCountFields"].values())
    control_count_field = slot_contract["assetUnitCountFields"]["controls"]
    component_fields = multi_contract["componentFields"]
    components = approved_graph_view[0] if approved_graph_view is not None else []
    approved_relations = approved_graph_view[1] if approved_graph_view is not None else []
    computed_visible_count = sum(
        component[component_fields["visualInstance"]] is True for component in components
    )
    computed_identity_ids = {
        component[component_fields["identityUnit"]]
        for component in components
        if component[component_fields["identityUnit"]] is not None
    }
    computed_upload_ids = {
        component[component_fields["uploadAsset"]]
        for component in components
        if component[component_fields["uploadAsset"]] is not None
    }
    computed_control_ids = {
        component[component_fields["control"]]
        for component in components
        if component[component_fields["control"]] is not None
    }
    slot_by_id = {slot["id"]: slot for slot in slots}
    semantic_role_by_key = {
        **slot_contract["semanticRoles"],
        **slot_contract["personAttributeRoles"],
    }
    allowed_control_bindings = {
        multi_contract["componentRoles"][component_role]: {
            (
                slot_contract["slotTypes"][binding["slotTypeRole"]],
                semantic_role_by_key[binding["semanticRoleKey"]],
            )
            for binding in bindings
        }
        for component_role, bindings in multi_contract[
            "approvedControlBindings"
        ].items()
    }
    uploads_by_control: dict[str, set[str]] = {}
    controls_by_upload: dict[str, set[str]] = {}
    for component in components:
        upload_id = component[component_fields["uploadAsset"]]
        control_id = component[component_fields["control"]]
        if upload_id is None or control_id is None:
            continue
        uploads_by_control.setdefault(control_id, set()).add(upload_id)
        controls_by_upload.setdefault(upload_id, set()).add(control_id)
    graph_counts = {
        slot_contract["assetUnitCountFields"]["visibleSubjects"]: computed_visible_count,
        slot_contract["assetUnitCountFields"]["identities"]: len(computed_identity_ids),
        slot_contract["assetUnitCountFields"]["uploads"]: len(computed_upload_ids),
        slot_contract["assetUnitCountFields"]["controls"]: len(computed_control_ids),
    }
    relation_fields = multi_contract["relationFields"]
    approved_component_by_id = {
        component[component_fields["identity"]]: component for component in components
    }
    approved_identity_relations_valid = _identity_relations_are_consistent(
        components, approved_relations, multi_contract
    )
    allowed_relation_role_pairs = {
        multi_contract["relationTypes"][relation_role]: {
            (
                multi_contract["componentRoles"][source_role],
                multi_contract["componentRoles"][target_role],
            )
            for source_role, target_role in role_pairs
        }
        for relation_role, role_pairs in multi_contract[
            "relationEndpointRoleKeyPairs"
        ].items()
    }
    approved_relation_roles_valid = all(
        (
            approved_component_by_id[relation[relation_fields["source"]]][
                component_fields["role"]
            ],
            approved_component_by_id[relation[relation_fields["target"]]][
                component_fields["role"]
            ],
        )
        in allowed_relation_role_pairs[relation[relation_fields["type"]]]
        for relation in approved_relations
    )
    approved_relation_types_by_component: dict[str, set[str]] = {
        component_id: set() for component_id in approved_component_by_id
    }
    for relation in approved_relations:
        approved_relation_types_by_component[relation[relation_fields["source"]]].add(
            relation[relation_fields["type"]]
        )
        approved_relation_types_by_component[relation[relation_fields["target"]]].add(
            relation[relation_fields["type"]]
        )
    identity_contract = rules["identityReplacementContract"]
    component_required_relation_types: dict[str, set[str]] = {}
    for dependency_role in multi_contract["approvedIdentityDependencyRoleKeys"]:
        required_relation_types = {
            multi_contract["relationTypes"][relation_role]
            for relation_role in identity_contract["dependencyRelationTypeKeys"][
                dependency_role
            ]
        }
        for component_role in identity_contract["dependencyComponentRoleKeys"][
            dependency_role
        ]:
            component_required_relation_types.setdefault(
                multi_contract["componentRoles"][component_role], set()
            ).update(required_relation_types)
    approved_component_relations_complete = all(
        component[component_fields["role"]]
        not in component_required_relation_types
        or bool(
            component_required_relation_types[component[component_fields["role"]]]
            & approved_relation_types_by_component[
                component[component_fields["identity"]]
            ]
        )
        for component in components
    )
    plan_operations = plan.get(multi_contract["planFields"]["imageOperations"], [])
    source_plan_graph_view = _component_graph_view(
        plan.get(multi_contract["planFields"]["componentGraph"]), rules
    )
    source_plan_components = (
        source_plan_graph_view[0] if source_plan_graph_view is not None else []
    )
    source_plan_relations = (
        source_plan_graph_view[1] if source_plan_graph_view is not None else []
    )
    source_plan_component_by_id = {
        component[component_fields["identity"]]: component
        for component in source_plan_components
    }
    source_plan_relation_by_id = {
        relation[relation_fields["identity"]]: relation
        for relation in source_plan_relations
    }
    operation_fields = multi_contract["operationFields"]
    operation_role_by_value = {
        value: role for role, value in multi_contract["operations"].items()
    }
    binding_fields = multi_contract["approvedOperationBindingFields"]
    approved_bindings = analysis.get(
        multi_contract["approvedFields"]["operationBindings"]
    )
    component_id_set = set(approved_component_by_id)
    plan_operation_ids = {
        operation[operation_fields["identity"]] for operation in plan_operations
    }
    approved_bindings_shape_valid = bool(
        isinstance(approved_bindings, list)
        and len(approved_bindings) == len(plan_operations)
        and all(
            isinstance(binding, dict)
            and set(binding) == set(binding_fields.values())
            and isinstance(binding.get(binding_fields["operationIdentity"]), str)
            and binding[binding_fields["operationIdentity"]].strip()
            and all(
                isinstance(binding.get(binding_fields[field]), list)
                and all(
                    isinstance(value, str) and value.strip()
                    for value in binding[binding_fields[field]]
                )
                and len(binding[binding_fields[field]])
                == len(set(binding[binding_fields[field]]))
                for field in ("targetComponents", "stableAnchors", "controls")
            )
            and binding[binding_fields["targetComponents"]]
            and binding[binding_fields["stableAnchors"]]
            and set(binding[binding_fields["targetComponents"]]) <= component_id_set
            and set(binding[binding_fields["stableAnchors"]]) <= component_id_set
            and not (
                set(binding[binding_fields["targetComponents"]])
                & set(binding[binding_fields["stableAnchors"]])
            )
            and set(binding[binding_fields["controls"]]) <= set(slot_by_id)
            and isinstance(binding.get(binding_fields["explanation"]), str)
            and binding[binding_fields["explanation"]].strip()
            for binding in approved_bindings
        )
        and {
            binding[binding_fields["operationIdentity"]]
            for binding in approved_bindings
        }
        == plan_operation_ids
    )
    if approved_bindings_shape_valid:
        approved_target_ids = [
            component_id
            for binding in approved_bindings
            for component_id in binding[binding_fields["targetComponents"]]
        ]
        approved_bindings_shape_valid = len(approved_target_ids) == len(
            set(approved_target_ids)
        )
    approved_binding_by_operation = (
        {
            binding[binding_fields["operationIdentity"]]: binding
            for binding in approved_bindings
        }
        if approved_bindings_shape_valid
        else {}
    )

    def approved_operation_is_complete(operation: dict[str, Any]) -> bool:
        binding = approved_binding_by_operation[
            operation[operation_fields["identity"]]
        ]
        operation_role = operation_role_by_value[
            operation[operation_fields["operation"]]
        ]
        requirement = multi_contract["operationRequirements"][operation_role]
        source_targets = [
            source_plan_component_by_id[target_id]
            for target_id in operation[operation_fields["targetRegions"]]
        ]
        source_anchors = [
            source_plan_component_by_id[anchor_id]
            for anchor_id in operation[operation_fields["stableAnchors"]]
        ]
        selected_targets = [
            approved_component_by_id[component_id]
            for component_id in binding[binding_fields["targetComponents"]]
        ]
        selected_anchors = [
            approved_component_by_id[component_id]
            for component_id in binding[binding_fields["stableAnchors"]]
        ]
        if (
            [component[component_fields["role"]] for component in selected_targets]
            != [component[component_fields["role"]] for component in source_targets]
            or [component[component_fields["role"]] for component in selected_anchors]
            != [component[component_fields["role"]] for component in source_anchors]
        ):
            return False
        if operation_role == "identityReplace":
            selected_identity_units = {
                component[component_fields["identityUnit"]]
                for component in selected_targets
            }
            if None in selected_identity_units or len(selected_identity_units) != 1:
                return False
            selected_identity = next(iter(selected_identity_units))
            if {
                component[component_fields["identity"]]
                for component in components
                if component[component_fields["identityUnit"]] == selected_identity
            } != {
                component[component_fields["identity"]]
                for component in selected_targets
            }:
                return False
        selected_ids = {
            component[component_fields["identity"]] for component in selected_targets
        }
        selected_anchor_ids = {
            component[component_fields["identity"]] for component in selected_anchors
        }
        selected_control_ids = {
            component[component_fields["control"]]
            for component in selected_targets
            if component[component_fields["control"]] is not None
        }
        components_using_selected_controls = {
            component[component_fields["identity"]]
            for component in components
            if component[component_fields["control"]] in selected_control_ids
        }
        requires_control = operation_role != "identityReplace" or any(
            slot["type"] == subject_upload_type for slot in slots
        )
        if selected_control_ids != set(binding[binding_fields["controls"]]) or (
            requires_control and not selected_control_ids
        ) or not components_using_selected_controls <= selected_ids:
            return False
        selected_container_ids = {
            component[component_fields["container"]]
            for component in selected_targets
            if component[component_fields["container"]] is not None
        }
        if requirement["targetContainersMustBeAnchors"] and not (
            selected_container_ids <= selected_anchor_ids
        ):
            return False
        selected_scope_ids = selected_ids | selected_anchor_ids | selected_container_ids
        required_relation_types = {
            multi_contract["relationTypes"][relation_role]
            for relation_role in requirement["requiredRelationTypeKeys"]
        }
        source_preserved_relations = [
            source_plan_relation_by_id[relation_id]
            for relation_id in operation[operation_fields["preservedRelations"]]
        ]
        required_relation_types.update(
            relation[relation_fields["type"]]
            for relation in source_preserved_relations
        )
        ordered_relation_type = multi_contract["relationTypes"]["orderedBefore"]
        scoped_approved_relations = [
            relation
            for relation in approved_relations
            if {
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            }
            <= selected_scope_ids
            and (
                bool(
                    {
                        relation[relation_fields["source"]],
                        relation[relation_fields["target"]],
                    }
                    & selected_ids
                )
                or relation[relation_fields["type"]] == ordered_relation_type
            )
        ]
        if not required_relation_types <= {
            relation[relation_fields["type"]]
            for relation in scoped_approved_relations
        }:
            return False

        source_to_approved_component = {
            **dict(
                zip(
                    operation[operation_fields["targetRegions"]],
                    binding[binding_fields["targetComponents"]],
                )
            ),
            **dict(
                zip(
                    operation[operation_fields["stableAnchors"]],
                    binding[binding_fields["stableAnchors"]],
                )
            ),
        }
        if any(
            relation[relation_fields["source"]] not in source_to_approved_component
            or relation[relation_fields["target"]]
            not in source_to_approved_component
            for relation in source_preserved_relations
        ):
            return False
        source_relation_signatures = [
            (
                relation[relation_fields["type"]],
                source_to_approved_component[relation[relation_fields["source"]]],
                source_to_approved_component[relation[relation_fields["target"]]],
            )
            for relation in source_preserved_relations
        ]
        approved_relation_signatures = [
            (
                relation[relation_fields["type"]],
                relation[relation_fields["source"]],
                relation[relation_fields["target"]],
            )
            for relation in scoped_approved_relations
        ]
        if any(
            approved_relation_signatures.count(signature)
            < source_relation_signatures.count(signature)
            for signature in set(source_relation_signatures)
        ):
            return False
        if requirement["requiresCompleteOrderedChain"]:
            return _complete_typed_relation_chain(
                selected_container_ids,
                approved_relations,
                multi_contract["relationTypes"]["orderedBefore"],
                relation_fields,
            )
        return True

    approved_operation_topology_complete = bool(
        source_plan_graph_view is not None
        and approved_bindings_shape_valid
        and all(
            approved_operation_is_complete(operation) for operation in plan_operations
        )
    )

    repeated_identity_type = multi_contract["relationTypes"]["repeatedIdentity"]

    def approved_repeated_subjects_are_connected() -> bool:
        subjects_by_identity: dict[str, set[str]] = {}
        for component in components:
            identity_unit = component[component_fields["identityUnit"]]
            if (
                identity_unit is not None
                and component[component_fields["role"]]
                == multi_contract["componentRoles"]["subject"]
            ):
                subjects_by_identity.setdefault(identity_unit, set()).add(
                    component[component_fields["identity"]]
                )
        repeated_edges = [
            relation
            for relation in approved_relations
            if relation[relation_fields["type"]] == repeated_identity_type
        ]
        for subject_ids in subjects_by_identity.values():
            if len(subject_ids) < 2:
                continue
            adjacency = {subject_id: set() for subject_id in subject_ids}
            for relation in repeated_edges:
                source_id = relation[relation_fields["source"]]
                target_id = relation[relation_fields["target"]]
                if source_id in subject_ids and target_id in subject_ids:
                    adjacency[source_id].add(target_id)
                    adjacency[target_id].add(source_id)
            visited: set[str] = set()
            pending = [next(iter(subject_ids))]
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(adjacency[current] - visited)
            if visited != subject_ids:
                return False
        return True
    approved_control_bindings_valid = all(
        component[component_fields["control"]] is None
        or (
            component[component_fields["control"]] in slot_by_id
            and (
                slot_by_id[component[component_fields["control"]]]["type"],
                slot_by_id[component[component_fields["control"]]]["semanticRole"],
            )
            in allowed_control_bindings.get(
                component[component_fields["role"]], set()
            )
        )
        for component in components
    )
    approved_graph_valid = bool(
        approved_graph_view is not None
        and approved_identity_relations_valid
        and approved_relation_roles_valid
        and approved_component_relations_complete
        and approved_operation_topology_complete
        and approved_repeated_subjects_are_connected()
        and approved_control_bindings_valid
        and computed_control_ids == {slot["id"] for slot in slots}
        and all(
            not component[component_fields["visualInstance"]]
            or component[component_fields["identityUnit"]] is not None
            for component in components
        )
        and all(
            component[component_fields["uploadAsset"]] is None
            or (
                component[component_fields["control"]] in slot_by_id
                and slot_by_id[component[component_fields["control"]]]["type"]
                == subject_upload_type
            )
            for component in components
        )
        and all(len(control_ids) == 1 for control_ids in controls_by_upload.values())
        and all(
            slot["type"] != subject_upload_type
            or bool(uploads_by_control.get(slot["id"]))
            for slot in slots
        )
        and all(
            len(upload_ids) <= SUBJECT_IMAGE_MAX_COUNT
            for upload_ids in uploads_by_control.values()
        )
    )
    asset_units_valid = bool(
        approved_graph_valid
        and isinstance(asset_units, dict)
        and set(asset_units) == {*count_fields, "evidence"}
        and all(
            isinstance(asset_units[field], int)
            and not isinstance(asset_units[field], bool)
            and asset_units[field] >= 0
            for field in count_fields
        )
        and all(asset_units[field] == value for field, value in graph_counts.items())
        and asset_units[control_count_field] == len(slots)
        and isinstance(asset_units.get("evidence"), str)
        and asset_units["evidence"].strip()
    )
    if not asset_units_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "组件图无效，或画面实例、身份、上传素材和控件四类数量没有独立准确计算。",
            {},
        )
    image_max_count_field = multi_contract["subjectImageMaxCountField"]
    for slot in slots:
        if slot["type"] == subject_upload_type:
            slot[image_max_count_field] = len(uploads_by_control[slot["id"]])
    default_preference = slot_contract["defaultValuePreference"]
    preference_exceptions = analysis.get("defaultValuePreferenceExceptionEvidence", {})
    preference_exceptions_valid = bool(
        isinstance(preference_exceptions, dict)
        and set(preference_exceptions) <= {slot["id"] for slot in slots}
        and all(
            isinstance(evidence, dict)
            and set(evidence) == {"reviewed", "reason"}
            and evidence.get("reviewed") is True
            and isinstance(evidence.get("reason"), str)
            and evidence["reason"].strip()
            for evidence in preference_exceptions.values()
        )
    )
    if not preference_exceptions_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位默认值语言或长度偏好例外证据无效。",
            {},
        )

    def exact_visible_text_evidence_is_valid(slot: dict[str, Any]) -> bool:
        evidence = slot.get("exactVisibleTextEvidence")
        return bool(
            slot.get("type") == slot_contract["slotTypes"]["visibleTextPrompt"]
            and slot.get("exactVisibleText") is True
            and isinstance(evidence, dict)
            and set(evidence) == {"approvedImageSha256", "visibleText", "evidence"}
            and evidence.get("approvedImageSha256") == analysis.get("visualFactSourceSha256")
            and evidence.get("visibleText") == slot.get("defaultValue")
            and isinstance(evidence.get("evidence"), str)
            and evidence["evidence"].strip()
        )

    invalid_exact_text_evidence = sorted(
        slot["id"]
        for slot in slots
        if (
            slot.get("exactVisibleText") is True
            and not exact_visible_text_evidence_is_valid(slot)
        )
        or (
            "exactVisibleTextEvidence" in slot
            and not exact_visible_text_evidence_is_valid(slot)
        )
    )
    if invalid_exact_text_evidence:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "精确画内文字槽必须绑定当前 Approved Template Image、默认值和可见证据。",
            {"slotIds": invalid_exact_text_evidence},
        )
    invalid_defaults = sorted(
        slot["id"]
        for slot in slots
        if not isinstance(slot.get("defaultValue"), str)
        or not slot["defaultValue"].strip()
        or (
            len(slot["defaultValue"].strip()) > default_preference["hardMaximum"]
            and not (
                default_preference["exactVisibleTextMayExceed"]
                and exact_visible_text_evidence_is_valid(slot)
            )
        )
        or (
            not default_preference["preferredMinimum"]
            <= len(slot["defaultValue"].strip())
            <= default_preference["preferredMaximum"]
            and slot["id"] not in preference_exceptions
        )
        or (
            default_preference["preferChinese"]
            and not CJK_CHARACTER.search(slot["defaultValue"].strip())
            and slot["id"] not in preference_exceptions
        )
    )
    if invalid_defaults:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位默认值为空、超出硬上限，或偏离中文与长度偏好但缺少审计例外。",
            {"slotIds": invalid_defaults},
        )
    prompt_template = analysis.get("promptTemplate")
    inline_bindings = PLACEHOLDER_WITH_DEFAULT.findall(prompt_template) if isinstance(prompt_template, str) else []
    inline_defaults_valid = bool(
        isinstance(prompt_template, str)
        and prompt_template.strip()
        and set(PLACEHOLDER.findall(prompt_template)) == {slot["id"] for slot in slots}
        and len(PLACEHOLDER.findall(prompt_template)) == len(inline_bindings)
        and all(
            inline_default == slot["defaultValue"]
            for slot in slots
            for binding_id, inline_default in inline_bindings
            if binding_id == slot["id"]
        )
    )
    if not inline_defaults_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "Prompt Template 的槽位绑定和内联默认值必须与槽位侧车完全一致。",
            {},
        )
    def suggestions_are_valid(slot: dict[str, Any]) -> bool:
        suggestions = slot.get("suggestions")
        normalized_suggestions = (
            [value.strip() for value in suggestions]
            if isinstance(suggestions, list) and all(isinstance(value, str) for value in suggestions)
            else []
        )
        return bool(
            isinstance(suggestions, list)
            and suggestions
            and all(isinstance(value, str) and value.strip() for value in suggestions)
            and len(normalized_suggestions) == len(set(normalized_suggestions))
            and slot.get("defaultValue", "").strip() not in normalized_suggestions
        )

    invalid_suggestion_slots = sorted(slot["id"] for slot in slots if not suggestions_are_valid(slot))
    if invalid_suggestion_slots:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "槽位推荐项包含空值、重复值或默认值。",
            {"slotIds": invalid_suggestion_slots},
        )
    missing_semantic_guards = sorted(
        slot["id"]
        for slot in slots
        if not slot.get("hiddenConflictTokens") or not slot.get("titleForbiddenTokens")
    )
    if missing_semantic_guards:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "高价值槽位缺少隐藏约束冲突词或最大差异标题禁用词。",
            {"slotIds": missing_semantic_guards},
        )
    review_field = rules["formalProjection"]["metadata"]["reviewReason"]
    needs_review = analysis.get(review_field)
    if needs_review is not None and (
        not isinstance(needs_review, str) or not needs_review.strip()
    ):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "needsReview 仅在确有人工复核原因时保留非空字符串。",
            {},
        )
    default_slot_values = {slot["id"]: slot["defaultValue"] for slot in slots}
    editable = {
        "artifactType": "editable-template-spec",
        "schemaVersion": rules["schemaVersion"],
        "visualFactSourceSha256": analysis["visualFactSourceSha256"],
        "title": analysis["neutralTitle"],
        "description": analysis["neutralDescription"],
        "slots": slots,
        "slotSuggestionPools": {slot["id"]: slot["suggestions"] for slot in slots},
        "promptTemplate": prompt_template,
        "freeEditableContent": analysis["freeEditableContent"],
        rules["visibleTextContract"]["analysisFields"]["regions"]: text_regions,
        rules["visibleTextContract"]["analysisFields"]["inventory"]: visible_text_inventory,
        "tags": analysis["tags"],
        "resolvedPromptContract": {
            "singleSourceField": "promptTemplate",
            "defaultSlotValues": default_slot_values,
            "defaultResolvedPrompt": _resolve_prompt(prompt_template, default_slot_values),
        },
    }
    if single_slot_valid:
        editable["singleSlotExceptionEvidence"] = single_slot_evidence
    if has_primary_subject and not any(
        slot["semanticRole"] == subject_role for slot in slots
    ):
        editable["subjectSlotOmissionEvidence"] = analysis["subjectSlotOmissionEvidence"]
    if subject_kind == person_kind:
        editable["subjectAttributeAssessments"] = analysis["subjectAttributeAssessments"]
    editable["assetUnitAnalysis"] = asset_units
    editable[multi_contract["approvedFields"]["componentGraph"]] = copy.deepcopy(
        approved_graph
    )
    if preference_exceptions:
        editable["defaultValuePreferenceExceptionEvidence"] = preference_exceptions
    if needs_review is not None:
        editable[review_field] = needs_review.strip()
    return editable


def _slot_to_input(slot: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    slot_types = rules["slotCompilationContract"]["slotTypes"]
    multi_contract = rules["multiInstanceContract"]
    if slot["type"] == slot_types["primarySubjectUpload"]:
        image_max_count = slot[multi_contract["subjectImageMaxCountField"]]
        return {
            "id": slot["id"],
            "type": slot_types["primarySubjectUpload"],
            "label": slot["label"],
            "required": False,
            "resolutionStrategy": "image_over_text",
            "text": {
                "placeholder": slot["placeholder"],
                "allowCustom": True,
                "defaultValue": slot["defaultValue"],
                "suggestions": slot["suggestions"],
            },
            "image": {
                "enabled": True,
                "promptValue": "用户上传的主体素材",
                "hint": (
                    "上传1张清晰主体图，按模板参考图的媒介与区域职责完整重绘"
                    if image_max_count == 1
                    else f"按画面顺序上传最多{image_max_count}张清晰主体图"
                ),
                "extract": (
                    "提取该主体可辨识的身份特征，并在模板参考图的媒介与造型体系中重绘。"
                    if image_max_count == 1
                    else "按上传顺序逐张提取主体身份特征，并匹配模板中的有序目标区域。"
                ),
                "maxCount": image_max_count,
                "minWidth": 256,
                "minHeight": 256,
                "private": True,
                "sourceOptions": ["upload", "recent_upload", "asset_library"],
            },
        }
    return {
        "id": slot["id"],
        "type": slot_types["freePrompt"],
        "label": slot["label"],
        "placeholder": slot["placeholder"],
        "required": False,
        "suggestions": slot["suggestions"],
    }


def _compile_hidden_spec(analysis: dict[str, Any], editable: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    prompt_enhancement = analysis.get("promptEnhancement")
    instruction = prompt_enhancement.get("instruction") if isinstance(prompt_enhancement, dict) else None
    locked_constraints = (
        prompt_enhancement.get("lockedConstraints") if isinstance(prompt_enhancement, dict) else None
    )
    preserve = prompt_enhancement.get("preserve") if isinstance(prompt_enhancement, dict) else None
    hidden_layers_valid = bool(
        isinstance(instruction, str)
        and instruction.strip()
        and isinstance(locked_constraints, list)
        and locked_constraints
        and all(isinstance(value, str) and value.strip() for value in locked_constraints)
        and len(locked_constraints) == len(set(locked_constraints))
        and isinstance(preserve, list)
        and preserve
        and all(isinstance(value, str) and value.strip() for value in preserve)
        and len(preserve) == len(set(preserve))
        and set(locked_constraints).isdisjoint(preserve)
    )
    if not hidden_layers_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "instruction、lockedConstraints 与 preserve 必须完整，且呈现维度和语义锚点职责不可重复。",
            {},
        )
    forbidden = [term for term in rules["prompt"]["forbiddenInstructionTerms"] if term in instruction]
    if len(instruction) > rules["prompt"]["instructionMaxCharacters"] or forbidden:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "instruction 超出长度限制或包含隐藏层禁用内容。",
            {"characters": len(instruction), "forbiddenTerms": forbidden},
        )
    return {
        "artifactType": "hidden-template-spec",
        "schemaVersion": rules["schemaVersion"],
        "visualFactSourceSha256": analysis["visualFactSourceSha256"],
        "inputSchema": [_slot_to_input(slot, rules) for slot in editable["slots"]],
        "promptEnhancement": {
            "stageKey": "gallery.prompt_rewrite",
            "instruction": instruction,
            "referenceField": "referenceImage",
            "lockedConstraints": locked_constraints,
            "preserve": preserve,
            "output": {"format": "json", "promptField": "finalPrompt"},
        },
    }


def _compile_draft(
    template_key: str,
    image_size: str,
    editable: dict[str, Any],
    hidden: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    formal_contract = rules["formalProjection"]
    top_level = formal_contract["topLevel"]
    tags_field = formal_contract["metadata"]["classificationTags"]
    review_field = formal_contract["metadata"]["reviewReason"]
    metadata = {tags_field: editable["tags"]}
    if review_field in editable:
        metadata[review_field] = editable[review_field]
    return {
        top_level["templateKey"]: template_key,
        top_level["lifecycleStatus"]: formal_contract["statusValues"]["draft"],
        top_level["userTitle"]: editable["title"],
        top_level["userDescription"]: editable["description"],
        top_level["outputImageSize"]: image_size,
        top_level["userPromptTemplate"]: editable["promptTemplate"],
        top_level["userInputSchema"]: hidden["inputSchema"],
        top_level["hiddenPromptEnhancement"]: hidden["promptEnhancement"],
        top_level["formalMetadata"]: metadata,
    }


SENTENCE_PUNCTUATION = re.compile(r"[，。！？；,.!?;]")


def _resolve_prompt(prompt_template: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), prompt_template)


def _semantic_audit_payload(
    draft: dict[str, Any], editable: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    top_level = rules["formalProjection"]["topLevel"]
    text_analysis_fields = rules["visibleTextContract"]["analysisFields"]
    return copy.deepcopy({
        top_level["userTitle"]: draft[top_level["userTitle"]],
        top_level["userPromptTemplate"]: draft[top_level["userPromptTemplate"]],
        top_level["hiddenPromptEnhancement"]: draft[top_level["hiddenPromptEnhancement"]],
        "freeEditableContent": editable["freeEditableContent"],
        text_analysis_fields["regions"]: editable[text_analysis_fields["regions"]],
        text_analysis_fields["inventory"]: editable[text_analysis_fields["inventory"]],
        "slots": [
            {
                "id": slot["id"],
                "type": slot["type"],
                "semanticRole": slot["semanticRole"],
                "label": slot["label"],
                "placeholder": slot["placeholder"],
                "defaultValue": slot["defaultValue"],
                "suggestions": slot["suggestions"],
            }
            for slot in editable["slots"]
        ],
    })


def _validation_report(
    draft: dict[str, Any],
    editable: dict[str, Any],
    plan: dict[str, Any],
    source_analysis: dict[str, Any],
    review: dict[str, Any],
    semantic_audit: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    top_level = rules["formalProjection"]["topLevel"]
    title_field = top_level["userTitle"]
    prompt_field = top_level["userPromptTemplate"]
    input_schema_field = top_level["userInputSchema"]
    prompt_enhancement_field = top_level["hiddenPromptEnhancement"]
    description_field = top_level["userDescription"]
    schema = _load_json(GALLERY_SCHEMA_PATH)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(draft), key=lambda item: list(item.path)
    )
    input_ids = {item["id"] for item in draft[input_schema_field]}
    referenced_ids = set(PLACEHOLDER.findall(draft[prompt_field]))
    missing_placeholders = sorted(input_ids - referenced_ids)
    unknown_placeholders = sorted(referenced_ids - input_ids)
    missing_free_editable_content = sorted(
        value for value in editable.get("freeEditableContent", []) if value not in draft[prompt_field]
    )
    default_values = {slot["id"]: slot["defaultValue"] for slot in editable["slots"]}
    resolved_prompts = [("defaults", _resolve_prompt(draft[prompt_field], default_values))]
    for slot in editable["slots"]:
        for suggestion in slot.get("suggestions", []):
            scenario_values = {**default_values, slot["id"]: suggestion}
            resolved_prompts.append(
                (f"{slot['id']}={suggestion}", _resolve_prompt(draft[prompt_field], scenario_values))
            )
    unnatural_resolved_prompts = sorted(
        label
        for label, resolved in resolved_prompts
        if PLACEHOLDER.search(resolved) or len(resolved.strip()) < 12 or SENTENCE_PUNCTUATION.search(resolved) is None
    )
    source_leaks = sorted(
        claim
        for claim in source_analysis.get("forbiddenLegacyClaims", [])
        if any(claim in text for text in _deep_strings(draft))
    )
    forbidden_keys = sorted(
        _deep_keys(draft) & set(rules["formalProjection"]["forbiddenKeys"].values())
    )
    forbidden_values = _forbidden_formal_values(draft, rules)
    production_terms = sorted(
        term
        for term in rules["prompt"]["forbiddenProductionTerms"]
        if any(term in text for text in _deep_strings(draft))
    )
    slot_values = {
        value
        for slot in editable["slots"]
        for value in [slot.get("defaultValue", ""), *slot.get("suggestions", [])]
        if value
    }
    free_editable_values = {value for value in editable.get("freeEditableContent", []) if value}
    hidden_text = " ".join(_deep_strings(draft[prompt_enhancement_field]))
    open_content_conflicts = sorted(
        value for value in slot_values | free_editable_values if value in hidden_text
    )
    open_axis_conflicts = sorted(
        token
        for slot in editable["slots"]
        for token in slot.get("hiddenConflictTokens", [])
        if token in hidden_text
    )
    title_slot_leaks = sorted(value for value in slot_values if value in draft[title_field])
    title_forbidden_tokens = sorted(
        token
        for slot in editable["slots"]
        for token in slot.get("titleForbiddenTokens", [])
        if token in draft[title_field]
    )
    identity_contract = rules["identityReplacementContract"]
    identity_plan_fields = identity_contract["planFields"]
    planned_identity_terms = plan.get(identity_plan_fields["neutralityTerms"], [])
    primary_subject_role = rules["slotCompilationContract"]["semanticRoles"]["primarySubject"]
    subject_upload_type = rules["slotCompilationContract"]["slotTypes"][
        "primarySubjectUpload"
    ]
    subject_slot_ids = {
        slot["id"]
        for slot in editable["slots"]
        if slot.get("type") == subject_upload_type
    }
    identity_terms = planned_identity_terms if subject_slot_ids else []
    text_contract = rules["visibleTextContract"]
    text_region_fields = text_contract["regionFields"]
    text_evidence_fields = text_contract["exactEvidenceFields"]
    identity_text_regions = [
        region
        for region in editable[text_contract["analysisFields"]["regions"]]
        if region.get(text_region_fields["valueClass"])
        == text_contract["valueClasses"]["identityRelated"]
    ]
    identity_neutrality_applicable = bool(
        subject_slot_ids and (identity_terms or identity_text_regions)
    )
    non_identity_prompt_content = PLACEHOLDER_WITH_DEFAULT.sub(
        lambda match: "" if match.group(1) in subject_slot_ids else match.group(0),
        draft[prompt_field],
    )
    identity_neutrality_texts = [
        draft[title_field],
        draft[description_field],
        non_identity_prompt_content,
        *_deep_strings(draft[prompt_enhancement_field]),
        *editable.get("freeEditableContent", []),
        *[
            value
            for slot in editable["slots"]
            if slot.get("semanticRole") != primary_subject_role
            for value in [
                slot.get("label", ""),
                slot.get("placeholder", ""),
                slot.get("defaultValue", ""),
                *slot.get("suggestions", []),
            ]
            if isinstance(value, str)
        ],
    ]
    identity_neutrality_leaks = sorted(
        term
        for term in identity_terms
        if isinstance(term, str)
        and term
        and any(term in text for text in identity_neutrality_texts)
    )
    audited_content_sha = _sha_bytes(_canonical_bytes(_semantic_audit_payload(draft, editable, rules)))
    semantic_audit_roles = rules["semanticAuditChecks"]
    semantic_audit_requirements = tuple(semantic_audit_roles.values())
    required_check_fields = {contract["check"] for contract in semantic_audit_requirements}
    required_evidence_fields = {contract["evidence"] for contract in semantic_audit_requirements}
    semantic_checks_payload = semantic_audit.get("checks")
    semantic_evidence_payload = semantic_audit.get("evidence")
    evidence = semantic_evidence_payload if isinstance(semantic_evidence_payload, dict) else {}

    def unique_nonempty_strings(value: Any) -> bool:
        return bool(
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item.strip() for item in value)
            and len(value) == len(set(value))
        )

    resolved_cases_field = semantic_audit_roles["resolvedPrompts"]["evidence"]
    open_axes_field = semantic_audit_roles["openAxes"]["evidence"]
    maximum_difference_field = semantic_audit_roles["maximumDifference"]["evidence"]
    suggestion_reviews_field = semantic_audit_roles["slotSuggestions"]["evidence"]
    instruction_scope_field = semantic_audit_roles["instructionScope"]["evidence"]
    hidden_responsibility_field = semantic_audit_roles["hiddenLayerResponsibilities"]["evidence"]
    identity_neutrality_field = semantic_audit_roles["identityNeutrality"]["evidence"]
    visible_text_classification_field = semantic_audit_roles["visibleTextClassification"]["evidence"]
    resolved_cases = evidence.get(resolved_cases_field)
    reviewed_open_axes = evidence.get(open_axes_field)
    maximum_difference_inputs = evidence.get(maximum_difference_field)
    suggestion_reviews = evidence.get(suggestion_reviews_field)
    instruction_scope_review = evidence.get(instruction_scope_field)
    hidden_responsibility_review = evidence.get(hidden_responsibility_field)
    identity_neutrality_review = evidence.get(identity_neutrality_field)
    visible_text_classification_review = evidence.get(visible_text_classification_field)
    identity_neutrality_fields = identity_contract["neutralityAuditFields"]
    text_audit_fields = text_contract["semanticAuditFields"]
    text_decision_fields = text_contract["semanticDecisionFields"]
    expected_text_decisions = {
        (
            region[text_region_fields["identity"]],
            region[text_region_fields["role"]],
            region[text_region_fields["action"]],
            region[text_region_fields["valueClass"]],
            region[text_region_fields["exactTextEvidence"]][
                text_evidence_fields["language"]
            ],
            tuple(
                region[text_region_fields["exactTextEvidence"]][
                    text_evidence_fields["tokens"]
                ]
            ),
        )
        for region in editable[text_contract["analysisFields"]["regions"]]
    }
    observed_text_decisions = set()
    if isinstance(visible_text_classification_review, dict):
        raw_text_decisions = visible_text_classification_review.get(
            text_audit_fields["decisions"]
        )
        for decision in raw_text_decisions if isinstance(raw_text_decisions, list) else []:
            if isinstance(decision, dict):
                observed_decision = (
                    decision.get(text_region_fields["identity"]),
                    decision.get(text_region_fields["role"]),
                    decision.get(text_region_fields["action"]),
                    decision.get(text_region_fields["valueClass"]),
                    decision.get(text_decision_fields["observedLanguage"]),
                    tuple(decision.get(text_decision_fields["observedTokens"], []))
                    if isinstance(
                        decision.get(text_decision_fields["observedTokens"]), list
                    )
                    and all(
                        isinstance(value, str)
                        for value in decision[text_decision_fields["observedTokens"]]
                    )
                    else None,
                )
                if all(isinstance(value, str) for value in observed_decision[:5]) and isinstance(
                    observed_decision[5], tuple
                ):
                    observed_text_decisions.add(observed_decision)
    identity_neutral_region_ids = {
        region[text_region_fields["identity"]]
        for region in identity_text_regions
    }
    slot_origin_fields = text_contract["slotOriginFields"]
    slot_origin_decisions = (
        visible_text_classification_review.get(text_audit_fields["slotOrigins"])
        if isinstance(visible_text_classification_review, dict)
        else None
    )
    expected_slot_ids = {slot["id"] for slot in editable["slots"]}
    region_by_id = {
        region[text_region_fields["identity"]]: region
        for region in editable[text_contract["analysisFields"]["regions"]]
    }

    def required_slot_origin(slot_id: str) -> str | None:
        return next(
            (
                region[text_region_fields["identity"]]
                for region in region_by_id.values()
                if region.get(text_region_fields["action"])
                == text_contract["actions"]["openSlot"]
                and region.get(text_region_fields["slotIdentity"]) == slot_id
            ),
            None,
        )

    slot_origin_evidence_valid = bool(
        isinstance(slot_origin_decisions, list)
        and len(slot_origin_decisions) == len(expected_slot_ids)
        and all(
            isinstance(decision, dict)
            and set(decision) == set(slot_origin_fields.values())
            and isinstance(decision.get(slot_origin_fields["slotIdentity"]), str)
            and (
                decision.get(slot_origin_fields["originRegionIdentity"]) is None
                or isinstance(
                    decision.get(slot_origin_fields["originRegionIdentity"]), str
                )
            )
            and isinstance(decision.get(slot_origin_fields["explanation"]), str)
            and decision[slot_origin_fields["explanation"]].strip()
            for decision in slot_origin_decisions
        )
        and {
            decision[slot_origin_fields["slotIdentity"]]
            for decision in slot_origin_decisions
        }
        == expected_slot_ids
        and all(
            (
                required_slot_origin(decision[slot_origin_fields["slotIdentity"]])
                is None
                or decision[slot_origin_fields["originRegionIdentity"]]
                == required_slot_origin(decision[slot_origin_fields["slotIdentity"]])
            )
            and (
                decision[slot_origin_fields["originRegionIdentity"]] is None
                or (
                    decision[slot_origin_fields["originRegionIdentity"]] in region_by_id
                    and region_by_id[
                        decision[slot_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["action"])
                    == text_contract["actions"]["openSlot"]
                    and region_by_id[
                        decision[slot_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["slotIdentity"])
                    == decision[slot_origin_fields["slotIdentity"]]
                )
            )
            for decision in slot_origin_decisions
        )
    )
    free_origin_fields = text_contract["freeContentOriginFields"]
    free_origin_decisions = (
        visible_text_classification_review.get(text_audit_fields["freeContentOrigins"])
        if isinstance(visible_text_classification_review, dict)
        else None
    )
    expected_free_content = editable.get("freeEditableContent", [])

    def required_free_content_origin(value: str) -> str | None:
        return next(
            (
                region[text_region_fields["identity"]]
                for region in region_by_id.values()
                if region.get(text_region_fields["action"])
                == text_contract["actions"]["freeEditable"]
                and region.get(text_region_fields["selectedText"]) == value
            ),
            None,
        )

    free_origin_evidence_valid = bool(
        isinstance(free_origin_decisions, list)
        and len(free_origin_decisions) == len(expected_free_content)
        and all(
            isinstance(decision, dict)
            and set(decision) == set(free_origin_fields.values())
            and isinstance(decision.get(free_origin_fields["content"]), str)
            and (
                decision.get(free_origin_fields["originRegionIdentity"]) is None
                or isinstance(
                    decision.get(free_origin_fields["originRegionIdentity"]), str
                )
            )
            and isinstance(decision.get(free_origin_fields["explanation"]), str)
            and decision[free_origin_fields["explanation"]].strip()
            for decision in free_origin_decisions
        )
        and sorted(
            decision[free_origin_fields["content"]]
            for decision in free_origin_decisions
        )
        == sorted(expected_free_content)
        and all(
            (
                required_free_content_origin(decision[free_origin_fields["content"]])
                is None
                or decision[free_origin_fields["originRegionIdentity"]]
                == required_free_content_origin(decision[free_origin_fields["content"]])
            )
            and (
                decision[free_origin_fields["originRegionIdentity"]] is None
                or (
                    decision[free_origin_fields["originRegionIdentity"]] in region_by_id
                    and region_by_id[
                        decision[free_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["action"])
                    == text_contract["actions"]["freeEditable"]
                    and region_by_id[
                        decision[free_origin_fields["originRegionIdentity"]]
                    ].get(text_region_fields["selectedText"])
                    == decision[free_origin_fields["content"]]
                )
            )
            for decision in free_origin_decisions
        )
    )
    fixed_region_leaks = (
        visible_text_classification_review.get(text_audit_fields["fixedRegionLeaks"])
        if isinstance(visible_text_classification_review, dict)
        else None
    )
    fixed_region_leak_evidence_valid = bool(
        isinstance(fixed_region_leaks, list)
        and all(
            isinstance(region_id, str) and region_id.strip()
            for region_id in fixed_region_leaks
        )
        and len(fixed_region_leaks) == len(set(fixed_region_leaks))
        and not fixed_region_leaks
    )
    expected_resolved_cases = {label for label, _ in resolved_prompts}
    expected_open_axes = {slot["semanticRole"] for slot in editable["slots"]}
    maximum_difference_set = (
        set(maximum_difference_inputs) if unique_nonempty_strings(maximum_difference_inputs) else set()
    )
    prompt_rules = rules["prompt"]
    hidden_roles = prompt_rules["hiddenLayerRoles"]
    semantic_evidence_valid = bool(
        unique_nonempty_strings(resolved_cases)
        and set(resolved_cases) == expected_resolved_cases
        and unique_nonempty_strings(reviewed_open_axes)
        and set(reviewed_open_axes) == expected_open_axes
        and unique_nonempty_strings(maximum_difference_inputs)
        and all(
            maximum_difference_set & set(slot["suggestions"])
            for slot in editable["slots"]
        )
        and unique_nonempty_strings(suggestion_reviews)
        and set(suggestion_reviews) == expected_slot_ids
        and isinstance(instruction_scope_review, dict)
        and set(instruction_scope_review) == {"allowedSections", "outOfScopeContentDetected", "evidence"}
        and unique_nonempty_strings(instruction_scope_review.get("allowedSections"))
        and set(instruction_scope_review["allowedSections"])
        == set(prompt_rules["instructionAllowedSections"].values())
        and instruction_scope_review.get("outOfScopeContentDetected") is False
        and isinstance(instruction_scope_review.get("evidence"), str)
        and instruction_scope_review["evidence"].strip()
        and isinstance(hidden_responsibility_review, dict)
        and set(hidden_responsibility_review)
        == {"lockedConstraintsRole", "preserveRole", "overlapDetected", "evidence"}
        and hidden_responsibility_review.get("lockedConstraintsRole")
        == hidden_roles["lockedConstraints"]
        and hidden_responsibility_review.get("preserveRole") == hidden_roles["preserve"]
        and hidden_responsibility_review.get("overlapDetected") is False
        and isinstance(hidden_responsibility_review.get("evidence"), str)
        and hidden_responsibility_review["evidence"].strip()
        and isinstance(identity_neutrality_review, dict)
        and set(identity_neutrality_review) == set(identity_neutrality_fields.values())
        and identity_neutrality_review.get(identity_neutrality_fields["applicability"])
        is identity_neutrality_applicable
        and identity_neutrality_review.get(
            identity_neutrality_fields["specificIdentityDetected"]
        )
        is False
        and isinstance(
            identity_neutrality_review.get(identity_neutrality_fields["explanation"]), str
        )
        and identity_neutrality_review[identity_neutrality_fields["explanation"]].strip()
        and isinstance(visible_text_classification_review, dict)
        and set(visible_text_classification_review) == set(text_audit_fields.values())
        and isinstance(
            visible_text_classification_review.get(text_audit_fields["reviewedRegionIdentities"]),
            list,
        )
        and all(
            isinstance(region_id, str) and region_id.strip()
            for region_id in visible_text_classification_review[
                text_audit_fields["reviewedRegionIdentities"]
            ]
        )
        and len(
            visible_text_classification_review[
                text_audit_fields["reviewedRegionIdentities"]
            ]
        )
        == len(
            set(
                visible_text_classification_review[
                    text_audit_fields["reviewedRegionIdentities"]
                ]
            )
        )
        and set(
            visible_text_classification_review[text_audit_fields["reviewedRegionIdentities"]]
        )
        == {
            region[text_region_fields["identity"]]
            for region in editable[text_contract["analysisFields"]["regions"]]
        }
        and isinstance(
            visible_text_classification_review.get(text_audit_fields["decisions"]), list
        )
        and len(visible_text_classification_review[text_audit_fields["decisions"]])
        == len(expected_text_decisions)
        and observed_text_decisions == expected_text_decisions
        and slot_origin_evidence_valid
        and free_origin_evidence_valid
        and fixed_region_leak_evidence_valid
        and all(
            isinstance(decision, dict)
            and set(decision)
            == {
                text_region_fields["identity"],
                text_region_fields["role"],
                text_region_fields["action"],
                text_region_fields["valueClass"],
                text_decision_fields["observedLanguage"],
                text_decision_fields["observedTokens"],
                text_decision_fields["identityNeutral"],
                text_audit_fields["explanation"],
            }
            and isinstance(
                decision.get(text_decision_fields["identityNeutral"]), bool
            )
            and (
                not identity_neutrality_applicable
                or decision.get(text_region_fields["identity"])
                not in identity_neutral_region_ids
                or decision.get(text_decision_fields["identityNeutral"]) is True
            )
            and isinstance(decision.get(text_audit_fields["explanation"]), str)
            and decision[text_audit_fields["explanation"]].strip()
            for decision in visible_text_classification_review[text_audit_fields["decisions"]]
        )
        and visible_text_classification_review.get(text_audit_fields["complete"]) is True
        and isinstance(
            visible_text_classification_review.get(text_audit_fields["explanation"]), str
        )
        and visible_text_classification_review[text_audit_fields["explanation"]].strip()
    )

    semantic_audit_contract_valid = (
        semantic_audit.get("artifactType") == "semantic-audit"
        and semantic_audit.get("schemaVersion") == rules["schemaVersion"]
        and isinstance(semantic_checks_payload, dict)
        and set(semantic_checks_payload) == required_check_fields
        and isinstance(semantic_evidence_payload, dict)
        and set(semantic_evidence_payload) == required_evidence_fields
        and semantic_evidence_valid
    )
    semantic_audit_bound = (
        semantic_audit_contract_valid
        and semantic_audit.get("contentSha256") == audited_content_sha
        and semantic_audit.get("observedContentSha256") == audited_content_sha
    )
    semantic_audit_checks = {
        contract["check"]: semantic_audit.get("checks", {}).get(contract["check"]) is True
        for contract in semantic_audit_requirements
    }
    semantic_audit_passed = semantic_audit_bound and all(semantic_audit_checks.values())
    visual_evidence_fields = rules["visualReviewContract"]["evidenceFieldRoles"]
    layers = {
        "schema": {"pass": not errors, "evidence": [error.message for error in errors]},
        "semantic": {
            "pass": not source_leaks
            and not missing_placeholders
            and not unknown_placeholders
            and not missing_free_editable_content
            and not unnatural_resolved_prompts
            and not title_slot_leaks
            and not title_forbidden_tokens
            and not identity_neutrality_leaks
            and semantic_audit_passed,
            "evidence": {
                "sourceLeaks": source_leaks,
                "missingSlotBindings": missing_placeholders,
                "unknownSlotBindings": unknown_placeholders,
                "missingFreeEditableContent": missing_free_editable_content,
                "unnaturalResolvedPrompts": unnatural_resolved_prompts,
                "titleSlotLeaks": title_slot_leaks,
                "titleForbiddenTokens": title_forbidden_tokens,
                "identityNeutralityLeaks": identity_neutrality_leaks,
                "semanticAudit": {
                    "contractValid": semantic_audit_contract_valid,
                    "contentBound": semantic_audit_bound,
                    "checks": semantic_audit_checks,
                    "evidence": semantic_audit.get("evidence", {}),
                },
            },
        },
        "visualContract": {
            "pass": all(review[visual_evidence_fields["hardGates"]].values())
            and all(
                item["pass"]
                for item in review[visual_evidence_fields["visualDimensions"]].values()
            ),
            "evidence": {"reviewSha256": _sha_bytes(_json_bytes(review))},
        },
        "galleryContract": {
            "pass": not forbidden_keys
            and not forbidden_values
            and not production_terms
            and not open_content_conflicts
            and not open_axis_conflicts,
            "evidence": {
                "forbiddenKeys": forbidden_keys,
                "forbiddenValues": forbidden_values,
                "productionTerms": production_terms,
                "openContentConflicts": open_content_conflicts,
                "openAxisConflicts": open_axis_conflicts,
            },
        },
    }
    return {
        "artifactType": "validation-report",
        "schemaVersion": rules["schemaVersion"],
        "layers": layers,
        "pass": all(layer["pass"] for layer in layers.values()),
    }


def _formal_projection(draft: dict[str, Any], url: str, rules: dict[str, Any]) -> dict[str, Any]:
    contract = rules["formalProjection"]
    top_level = contract["topLevel"]
    metadata_field = top_level["formalMetadata"]
    cover_field = top_level["coverAsset"]
    reference_field = top_level["referenceAsset"]
    review_field = contract["metadata"]["reviewReason"]
    allowed_top_level = set(contract["topLevel"].values())
    unexpected_top_level = sorted(set(draft) - allowed_top_level)
    metadata = draft.get(metadata_field)
    allowed_metadata = set(contract["metadata"].values())
    recognized_sidecars = set(contract["recognizedMetadataSidecars"].values())
    unexpected_metadata = sorted(
        set(metadata) - allowed_metadata - recognized_sidecars
        if isinstance(metadata, dict)
        else []
    )
    needs_review = metadata.get(review_field) if isinstance(metadata, dict) else None
    source_valid = bool(
        not unexpected_top_level
        and isinstance(metadata, dict)
        and not unexpected_metadata
        and (
            review_field not in metadata
            or (isinstance(needs_review, str) and needs_review.strip())
        )
        and _public_asset_url_valid(url, rules)
    )
    if not source_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "正式投影源包含未知字段、无效复核原因或非 HTTPS 模板图 URL。",
            {
                "unexpectedTopLevel": unexpected_top_level,
                "unexpectedMetadata": unexpected_metadata,
            },
        )
    complete = copy.deepcopy(draft)
    complete[cover_field] = url
    complete[reference_field] = url
    projection = {
        key: complete[key]
        for key in contract["topLevel"].values()
        if key in complete
    }
    projection[metadata_field] = {
        key: complete[metadata_field][key]
        for key in contract["metadata"].values()
        if key in complete[metadata_field]
    }
    return projection


def _validate_final(record: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    schema = _load_json(GALLERY_SCHEMA_PATH)
    contract = rules["formalProjection"]
    top_level = contract["topLevel"]
    metadata_field = top_level["formalMetadata"]
    status_field = top_level["lifecycleStatus"]
    cover_field = top_level["coverAsset"]
    reference_field = top_level["referenceAsset"]
    review_field = contract["metadata"]["reviewReason"]
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(record), key=lambda item: list(item.path)
    )
    forbidden_keys = sorted(_deep_keys(record) & set(contract["forbiddenKeys"].values()))
    expected_top_level = set(contract["topLevel"].values())
    top_level_extra = sorted(set(record) - expected_top_level)
    top_level_missing = sorted(expected_top_level - set(record))
    metadata = record.get(metadata_field)
    metadata_extra = sorted(
        set(metadata) - set(contract["metadata"].values())
        if isinstance(metadata, dict)
        else []
    )
    forbidden_values = _forbidden_formal_values(record, rules)
    production_terms = sorted(
        term
        for term in rules["prompt"]["forbiddenProductionTerms"]
        if any(term in value for value in _deep_strings(record))
    )
    needs_review = metadata.get(review_field) if isinstance(metadata, dict) else None
    needs_review_valid = bool(
        isinstance(metadata, dict)
        and (
            review_field not in metadata
            or (
                isinstance(needs_review, str)
                and needs_review.strip()
                and record.get(status_field) == contract["statusValues"]["draft"]
            )
        )
    )
    cover = record.get(cover_field)
    reference_image = record.get(reference_field)
    cover_matches_reference = cover == reference_image
    asset_urls_valid = bool(
        _public_asset_url_valid(cover, rules)
        and _public_asset_url_valid(reference_image, rules)
    )
    passed = bool(
        not errors
        and not forbidden_keys
        and not top_level_extra
        and not top_level_missing
        and not metadata_extra
        and not forbidden_values
        and not production_terms
        and needs_review_valid
        and cover_matches_reference
        and asset_urls_valid
    )
    return {
        "artifactType": "final-validation-report",
        "schemaVersion": rules["schemaVersion"],
        "pass": passed,
        "schemaErrors": [error.message for error in errors],
        "forbiddenKeys": forbidden_keys,
        "topLevelExtra": top_level_extra,
        "topLevelMissing": top_level_missing,
        "metadataExtra": metadata_extra,
        "forbiddenValues": forbidden_values,
        "productionTerms": production_terms,
        "needsReviewValid": needs_review_valid,
        "coverMatchesReferenceImage": cover_matches_reference,
        "assetUrlsValid": asset_urls_valid,
    }


def formal_template_contract_valid(
    record: Any, rules: dict[str, Any]
) -> bool:
    """Return whether a persisted Gallery template satisfies the formal contract."""
    if not isinstance(record, dict):
        return False
    return bool(_validate_final(record, rules)["pass"])


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


def _run_single_production(
    request: dict[str, Any],
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None = None,
    prepared_source_analysis: dict[str, Any] | None = None,
    shared_policy_resolution: dict[str, Any] | None = None,
    preparation_stop: WorkflowStop | None = None,
) -> ProductionResult:
    """Run one independent Production Item through P0-P8."""

    rules = _load_json(RULES_PATH)
    release = _load_json(RELEASE_PATH)
    p0, p1, p2, p3, p4, p5, p6, p7, p8 = (item["phase"] for item in rules["productionPhases"])
    now = clock or (lambda: datetime.now(timezone.utc))
    timestamp = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    output_root_path = Path(output_root).resolve()
    schema = _load_json(GALLERY_SCHEMA_PATH)
    template_key = str(request.get("templateKey", ""))
    production_item_id = request.get("productionItemId")
    request_errors = _production_request_errors(request, rules, schema)
    if request_errors:
        return ProductionResult(
            "needs_input",
            str(production_item_id or "invalid-production-item"),
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="生产请求预检失败：" + "；".join(request_errors),
        )
    replacement_strategy = _normalize_replacement_strategy(request, rules)
    generation_options = _normalized_generation_options(request, rules)
    source_image = Path(request["sourceImage"]).resolve()
    if not source_image.is_file():
        raise FileNotFoundError(source_image)
    source_sha = _sha_file(source_image)
    replacement_strategy_identity: Any = replacement_strategy
    if shared_policy_resolution is not None:
        replacement_strategy_identity = {
            "replacementStrategy": replacement_strategy,
            "sharedPolicyResolution": shared_policy_resolution,
        }
    replacement_strategy_sha = _sha_bytes(
        _canonical_bytes(replacement_strategy_identity)
    )
    generation_options_sha = _sha_bytes(_canonical_bytes(generation_options))
    item_id = str(production_item_id or f"{template_key}-{source_sha[:12]}")
    output_dir = _isolated_output_dir(output_root_path, item_id)
    if output_dir is None:
        return ProductionResult(
            "needs_input",
            item_id,
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="Production Item 输出目录越出 output root。",
        )
    manifest_path = output_dir / "production-manifest.json"
    existing_pin: dict[str, Any] | None = None
    pin_path = output_dir / "production-pin.json"
    if pin_path.is_file():
        try:
            raw_pin = _load_json(pin_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            raw_pin = None
        if isinstance(raw_pin, dict):
            existing_pin = raw_pin
    runtime_diagnosis = doctor(REPO_ROOT, production_pin=existing_pin)
    diagnostic_fields = rules["releaseManagementContract"][
        "diagnosticFields"
    ]
    if not runtime_diagnosis["pass"]:
        return ProductionResult(
            "blocked",
            item_id,
            rules["resultStates"]["blocked"],
            output_dir,
            error_code=rules["errorCodes"]["versionDiagnosticFailure"],
            message="运行前 doctor 检查未通过："
            + "、".join(
                runtime_diagnosis[diagnostic_fields["errorCodes"]]
            ),
            resumed=manifest_path.is_file(),
        )
    resume_visual = False
    resume_generation = False
    resume_prepared_generation = False
    reuse_succeeded_generation = False
    resumed = False
    source_analysis: dict[str, Any]
    plan: dict[str, Any]
    generation_package: dict[str, Any]
    generation_task: dict[str, Any]
    generation_wal: dict[str, Any]
    generation_submission: dict[str, Any]
    if manifest_path.exists():
        try:
            existing = _load_json(manifest_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            existing = None
        if not isinstance(existing, dict):
            return ProductionResult(
                "blocked",
                item_id,
                rules["resultStates"]["blocked"],
                output_dir,
                error_code=rules["errorCodes"][
                    "productionItemIntegrityFailure"
                ],
                message="Production Manifest 无法读取或顶层形状无效。",
                resumed=True,
            )
        completed_artifacts = ("production-pin.json", "gallery-template.json", "final-validation-report.json")
        identity_errors = _production_item_integrity_errors(
            output_dir,
            existing,
            production_item_id=item_id,
            template_key=template_key,
            source_sha256=source_sha,
            replacement_strategy_sha256=replacement_strategy_sha,
            generation_options_sha256=generation_options_sha,
            required_artifacts=completed_artifacts if existing.get("state") == rules["resultStates"]["completed"] else (),
        )
        if shared_policy_resolution is not None:
            identity_errors.extend(
                _current_shared_policy_resolution_errors(
                    output_dir,
                    shared_policy_resolution,
                    rules,
                )
            )
        if existing.get("state") == rules["resultStates"]["completed"]:
            identity_errors.extend(_current_p2_artifact_errors(existing))
            identity_errors.extend(
                _current_generation_execution_errors(
                    output_dir,
                    existing,
                    source_sha,
                    generation_options,
                    rules,
                )
            )
            identity_errors.extend(
                _current_finalization_errors(output_dir, existing, rules)
            )
            identity_errors.extend(
                _current_item_fact_errors(output_dir, existing, rules)
            )
        existing_revision_for_wal = existing.get("revision")
        if (
            existing.get("state") != rules["resultStates"]["completed"]
            and isinstance(existing_revision_for_wal, int)
            and not isinstance(existing_revision_for_wal, bool)
            and existing.get("phase") != rules["productionPhases"][7]["phase"]
        ):
            current_wal_name = _revisioned_name(
                "generation-wal.json", existing_revision_for_wal
            )
            deferred_wal_digest_error = f"{current_wal_name} digest mismatch"
            if deferred_wal_digest_error in identity_errors:
                identity_errors.remove(deferred_wal_digest_error)
        if existing.get("state") == rules["resultStates"]["completed"]:
            revision = existing.get("revision")
            generation_fact_names = [
                _revisioned_name("generation-package.json", revision),
                *_revision_image_artifacts(existing, "generated-candidate-image", revision),
            ]
            changed_generation_facts = _changed_lineage_artifacts(
                output_dir,
                existing,
                generation_fact_names,
            )
            if changed_generation_facts:
                changed_name = changed_generation_facts[0]
                changed_path = output_dir / changed_name
                _append_invalidation_event(
                    existing,
                    rules,
                    reason_key="generationFactsChanged",
                    superseded_artifact=changed_name,
                    observed_sha256=_sha_file(changed_path) if changed_path.is_file() else None,
                    invalidated_artifacts=_artifact_descendants(existing, changed_name),
                    invalidated_from_phase=p2,
                    timestamp=timestamp,
                )
                existing["phase"] = p1
                existing["state"] = rules["resultStates"]["blocked"]
                existing["outcome"] = "blocked"
                existing["error"] = {
                    "code": rules["errorCodes"]["productionItemIntegrityFailure"],
                    "message": "上游生图事实摘要发生变化，P2 及下游产物已失效。",
                    "evidence": {"artifact": changed_name},
                }
                _persist_manifest(output_dir, existing)
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message=existing["error"]["message"],
                    resumed=True,
                )
            approved_names = _revision_image_artifacts(
                existing,
                "approved-template-image",
                revision,
            )
            changed_approved = _changed_lineage_artifacts(output_dir, existing, approved_names)
            if len(changed_approved) == 1:
                changed_name = changed_approved[0]
                changed_path = output_dir / changed_name
                _append_invalidation_event(
                    existing,
                    rules,
                    reason_key="approvedImageChanged",
                    superseded_artifact=changed_name,
                    observed_sha256=_sha_file(changed_path) if changed_path.is_file() else None,
                    invalidated_artifacts=_artifact_descendants(existing, changed_name),
                    invalidated_from_phase=p3,
                    timestamp=timestamp,
                )
                existing["phase"] = p2
                existing["state"] = rules["resultStates"]["blocked"]
                existing["outcome"] = "blocked"
                existing["error"] = {
                    "code": rules["errorCodes"]["productionItemIntegrityFailure"],
                    "message": "确认模板图摘要发生变化，依赖视觉事实已失效。",
                    "evidence": {"artifact": changed_name},
                }
                _persist_manifest(output_dir, existing)
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message=existing["error"]["message"],
                    resumed=True,
                )
        if existing.get("state") == rules["resultStates"]["completed"] and identity_errors:
            return ProductionResult(
                "blocked",
                item_id,
                rules["resultStates"]["blocked"],
                output_dir,
                error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                message="已完成 Production Item 的身份或产物谱系校验失败：" + "；".join(identity_errors),
            )
        if existing.get("state") == rules["resultStates"]["completed"]:
            return ProductionResult(
                "completed",
                item_id,
                rules["resultStates"]["completed"],
                output_dir,
                output_dir / "gallery-template.json",
                resumed=True,
            )
        if identity_errors and any(error.endswith("mismatch") for error in identity_errors):
            return ProductionResult(
                "blocked",
                item_id,
                rules["resultStates"]["blocked"],
                output_dir,
                error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                message="Production Item 请求身份与已有状态不一致：" + "；".join(identity_errors),
            )
        uploaded_phase = rules["productionPhases"][7]
        if existing.get("phase") == uploaded_phase["phase"] and existing.get("state") == uploaded_phase["state"]:
            recovery_errors = _production_item_integrity_errors(
                output_dir,
                existing,
                production_item_id=item_id,
                template_key=template_key,
                source_sha256=source_sha,
                replacement_strategy_sha256=replacement_strategy_sha,
                generation_options_sha256=generation_options_sha,
                required_artifacts=(
                    "production-pin.json",
                    "gallery-template.draft.json",
                    "validation-report.json",
                    "asset-receipt.json",
                ),
            )
            recovery_errors.extend(_current_p2_artifact_errors(existing))
            recovery_errors.extend(
                _current_generation_execution_errors(
                    output_dir,
                    existing,
                    source_sha,
                    generation_options,
                    rules,
                )
            )
            recovery_errors.extend(
                _current_item_fact_errors(output_dir, existing, rules)
            )
            if recovery_errors:
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message="P7 Production Item 的身份或产物谱系校验失败：" + "；".join(recovery_errors),
                )
            try:
                return _finalize_uploaded_item(output_dir, existing, rules, timestamp)
            except WorkflowStop as stop:
                existing["state"] = stop.state
                existing["outcome"] = stop.outcome
                existing["error"] = {
                    "code": stop.error_code,
                    "message": stop.message,
                    "evidence": stop.evidence,
                }
                _persist_manifest(output_dir, existing)
                return ProductionResult(
                    stop.outcome,
                    item_id,
                    stop.state,
                    output_dir,
                    error_code=stop.error_code,
                    message=stop.message,
                    resumed=True,
                )
        existing_error_code = existing.get("error", {}).get("code")
        existing_revision = existing.get("revision")
        if isinstance(existing_revision, int) and existing_error_code != rules["errorCodes"][
            "visualHardFailure"
        ]:
            generation_package_name = _revisioned_name(
                "generation-package.json", existing_revision
            )
            generation_task_name = _revisioned_name("generation-task.json", existing_revision)
            generation_wal_name = _revisioned_name("generation-wal.json", existing_revision)
            generation_task_path = output_dir / generation_task_name
            generation_wal_path = output_dir / generation_wal_name
            if generation_task_path.is_file() or generation_wal_path.is_file():
                staging_names = (
                    generation_package_name,
                    generation_task_name,
                    generation_wal_name,
                )
                if any(name not in existing["artifacts"] for name in staging_names):
                    staging_errors = _production_item_integrity_errors(
                        output_dir,
                        existing,
                        production_item_id=item_id,
                        template_key=template_key,
                        source_sha256=source_sha,
                        replacement_strategy_sha256=replacement_strategy_sha,
                        generation_options_sha256=generation_options_sha,
                        required_artifacts=(
                            "production-pin.json",
                            "source-analysis.json",
                            "replacement-plan.json",
                        ),
                    )
                    if not staging_errors:
                        (
                            staging_errors,
                            generation_package,
                            generation_task,
                            generation_wal,
                        ) = _adopt_pre_submit_generation_staging(
                            output_dir,
                            existing,
                            source_sha,
                            generation_options,
                            rules,
                            timestamp,
                            p2,
                        )
                    if staging_errors:
                        return ProductionResult(
                            "blocked",
                            item_id,
                            rules["resultStates"]["blocked"],
                            output_dir,
                            error_code=rules["errorCodes"][
                                "productionItemIntegrityFailure"
                            ],
                            message="P2 提交前 staging 恢复校验失败："
                            + "；".join(staging_errors),
                            resumed=True,
                        )
                recovery_errors = _production_item_integrity_errors(
                    output_dir,
                    existing,
                    production_item_id=item_id,
                    template_key=template_key,
                    source_sha256=source_sha,
                    replacement_strategy_sha256=replacement_strategy_sha,
                    generation_options_sha256=generation_options_sha,
                    required_artifacts=(
                        "production-pin.json",
                        "source-analysis.json",
                        "replacement-plan.json",
                        generation_package_name,
                        generation_task_name,
                        generation_wal_name,
                    ),
                )
                wal_digest_error = f"{generation_wal_name} digest mismatch"
                wal_manifest_lag = wal_digest_error in recovery_errors
                if wal_manifest_lag:
                    recovery_errors.remove(wal_digest_error)
                if not generation_task_path.is_file() or not generation_wal_path.is_file():
                    recovery_errors.append("generation task or WAL file missing")
                if not recovery_errors:
                    (
                        execution_errors,
                        generation_package,
                        generation_task,
                        generation_wal,
                    ) = _load_generation_execution_evidence(
                            output_dir,
                            generation_package_name,
                            generation_task_name,
                            generation_wal_name,
                            source_sha,
                            existing_revision,
                            generation_options,
                            rules,
                        )
                    recovery_errors.extend(execution_errors)
                    if wal_manifest_lag and not execution_errors:
                        recorded_wal = existing["artifacts"].get(generation_wal_name)
                        previous_wal_sha = generation_wal[
                            rules["generationExecutionContract"]["walFields"][
                                "previousWalSha256"
                            ]
                        ]
                        if (
                            not isinstance(recorded_wal, dict)
                            or previous_wal_sha != recorded_wal.get("sha256")
                        ):
                            recovery_errors.append(
                                "generation WAL does not continue the recorded digest"
                            )
                        else:
                            _record_artifact(
                                existing,
                                output_dir,
                                generation_wal_name,
                                p2,
                                [generation_task_name],
                            )
                            _persist_manifest(output_dir, existing)
                if recovery_errors:
                    return ProductionResult(
                        "blocked",
                        item_id,
                        rules["resultStates"]["blocked"],
                        output_dir,
                        error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                        message="生成任务恢复前的身份、WAL 或谱系校验失败："
                        + "；".join(recovery_errors),
                        resumed=True,
                    )
                execution_contract = rules["generationExecutionContract"]
                wal_fields = execution_contract["walFields"]
                failure_class = generation_wal[wal_fields["failureClass"]]
                provider_request_id = generation_wal[
                    wal_fields["providerRequestIdentity"]
                ]
                wal_status = generation_wal[wal_fields["status"]]
                can_resume_poll = (
                    wal_status == execution_contract["walStatuses"]["submitted"]
                    or failure_class
                    == execution_contract["failureClasses"]["retryable"]
                )
                can_resume_prepared = (
                    wal_status == execution_contract["walStatuses"]["prepared"]
                )
                can_reuse_candidate = (
                    wal_status == execution_contract["walStatuses"]["succeeded"]
                )
                if can_resume_prepared:
                    manifest = existing
                    source_analysis = _load_json(output_dir / "source-analysis.json")
                    plan = _load_json(output_dir / "replacement-plan.json")
                    manifest["state"] = next(
                        item["state"]
                        for item in rules["productionPhases"]
                        if item["phase"] == p1
                    )
                    manifest["outcome"] = None
                    manifest.pop("error", None)
                    _persist_manifest(output_dir, manifest)
                    resume_prepared_generation = True
                    resumed = True
                elif (
                    (can_resume_poll or can_reuse_candidate)
                    and isinstance(provider_request_id, str)
                    and provider_request_id.strip()
                ):
                    if can_reuse_candidate:
                        task_fields = execution_contract["taskFields"]
                        intent_fields = execution_contract["requestIntentFields"]
                        output_format = generation_task[task_fields["requestIntent"]][
                            intent_fields["outputFormat"]
                        ]
                        output_format_role = next(
                            role
                            for role, value in execution_contract[
                                "outputFormats"
                            ].items()
                            if value == output_format
                        )
                        expected_candidate_name = _revisioned_name(
                            "evidence/generated-candidate-image"
                            + execution_contract["outputFormatExtensions"][
                                output_format_role
                            ],
                            existing_revision,
                        )
                        expected_candidate_path = output_dir / expected_candidate_name
                        if (
                            expected_candidate_name not in existing["artifacts"]
                            and _file_matches_sha(
                                expected_candidate_path,
                                generation_wal[wal_fields["outputSha256"]],
                            )
                        ):
                            _record_artifact(
                                existing,
                                output_dir,
                                expected_candidate_name,
                                p2,
                                [
                                    generation_package_name,
                                    generation_task_name,
                                    generation_wal_name,
                                ],
                            )
                            _persist_manifest(output_dir, existing)
                        recovery_errors.extend(
                            _current_generation_execution_errors(
                                output_dir,
                                existing,
                                source_sha,
                                generation_options,
                                rules,
                            )
                        )
                        if recovery_errors:
                            return ProductionResult(
                                "blocked",
                                item_id,
                                rules["resultStates"]["blocked"],
                                output_dir,
                                error_code=rules["errorCodes"][
                                    "productionItemIntegrityFailure"
                                ],
                                message="成功生成任务的本地候选图或 WAL 对账失败："
                                + "；".join(recovery_errors),
                                resumed=True,
                            )
                    submission_fields = execution_contract["submissionFields"]
                    generation_submission = {
                        submission_fields["status"]: execution_contract[
                            "submissionStatuses"
                        ]["submitted"],
                        submission_fields["provider"]: generation_wal[
                            wal_fields["provider"]
                        ],
                        submission_fields["model"]: generation_wal[wal_fields["model"]],
                        submission_fields["providerRequestIdentity"]: provider_request_id,
                        submission_fields["failureClass"]: None,
                        submission_fields["failureReason"]: None,
                    }
                    manifest = existing
                    source_analysis = _load_json(output_dir / "source-analysis.json")
                    plan = _load_json(output_dir / "replacement-plan.json")
                    manifest["state"] = next(
                        item["state"]
                        for item in rules["productionPhases"]
                        if item["phase"] == p1
                    )
                    manifest["outcome"] = None
                    manifest.pop("error", None)
                    if can_resume_poll:
                        generation_wal[wal_fields["status"]] = execution_contract[
                            "walStatuses"
                        ]["submitted"]
                        generation_wal[wal_fields["failureClass"]] = None
                        generation_wal[wal_fields["failureReason"]] = None
                        generation_wal[wal_fields["updatedAt"]] = timestamp
                        _write_generation_wal(generation_wal_path, generation_wal, rules)
                        _record_artifact(
                            manifest,
                            output_dir,
                            generation_wal_name,
                            p2,
                            [generation_task_name],
                        )
                    _persist_manifest(output_dir, manifest)
                    resume_generation = True
                    reuse_succeeded_generation = can_reuse_candidate
                    resumed = True
                else:
                    if failure_class not in execution_contract["failureClasses"].values():
                        failure_class = execution_contract["failureClasses"][
                            "submissionUnknown"
                        ]
                    stop = _generation_failure_stop(
                        failure_class,
                        generation_wal[wal_fields["failureReason"]]
                        or "provider submission state is uncertain",
                        rules,
                        {
                            "taskId": generation_wal[wal_fields["taskIdentity"]],
                            "providerRequestId": provider_request_id,
                        },
                    )
                    existing["state"] = stop.state
                    existing["outcome"] = stop.outcome
                    existing["error"] = {
                        "code": stop.error_code,
                        "message": stop.message,
                        "evidence": stop.evidence,
                    }
                    _persist_manifest(output_dir, existing)
                    return ProductionResult(
                        stop.outcome,
                        item_id,
                        stop.state,
                        output_dir,
                        error_code=stop.error_code,
                        message=stop.message,
                        resumed=True,
                    )
        if existing.get("error", {}).get("code") == rules["errorCodes"]["visualHardFailure"]:
            previous_revision = existing.get("revision")
            if not isinstance(previous_revision, int) or previous_revision < 1:
                recovery_errors = ["manifest revision invalid"]
            else:
                previous_package_name = _revisioned_name("generation-package.json", previous_revision)
                previous_task_name = _revisioned_name("generation-task.json", previous_revision)
                previous_wal_name = _revisioned_name("generation-wal.json", previous_revision)
                previous_review_name = _revisioned_name("visual-review.json", previous_revision)
                recovery_errors = _production_item_integrity_errors(
                    output_dir,
                    existing,
                    production_item_id=item_id,
                    template_key=template_key,
                    source_sha256=source_sha,
                    replacement_strategy_sha256=replacement_strategy_sha,
                    generation_options_sha256=generation_options_sha,
                    required_artifacts=(
                        "production-pin.json",
                        "source-analysis.json",
                        "replacement-plan.json",
                        previous_package_name,
                        previous_task_name,
                        previous_wal_name,
                        previous_review_name,
                    ),
                )
            if recovery_errors:
                return ProductionResult(
                    "blocked",
                    item_id,
                    rules["resultStates"]["blocked"],
                    output_dir,
                    error_code=rules["errorCodes"]["productionItemIntegrityFailure"],
                    message="P2 重做前的身份或产物谱系校验失败：" + "；".join(recovery_errors),
                    resumed=True,
                )
            manifest = existing
            source_analysis = _load_json(output_dir / "source-analysis.json")
            plan = _load_json(output_dir / "replacement-plan.json")
            previous_package = _load_json(output_dir / previous_package_name)
            previous_review = _load_json(output_dir / previous_review_name)
            manifest["revision"] = previous_revision + 1
            manifest["state"] = next(
                item["state"] for item in rules["productionPhases"] if item["phase"] == p1
            )
            manifest["outcome"] = None
            manifest.pop("error", None)
            generation_package = _compile_redo_generation_package(
                previous_package,
                previous_review,
                manifest["revision"],
            )
            replacement_package_name = _revisioned_name(
                "generation-package.json", manifest["revision"]
            )
            _append_invalidation_event(
                manifest,
                rules,
                reason_key="generationFactsChanged",
                superseded_artifact=previous_package_name,
                replacement_artifact=replacement_package_name,
                replacement_sha256=_sha_bytes(_json_bytes(generation_package)),
                invalidated_artifacts=_artifact_descendants(manifest, previous_package_name),
                invalidated_from_phase=p2,
                timestamp=timestamp,
            )
            resume_visual = True
            resumed = True
            _persist_manifest(output_dir, manifest)
    if not resume_visual and not resume_generation and not resume_prepared_generation:
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "artifactType": "production-manifest",
            "schemaVersion": rules["schemaVersion"],
            "productionItemId": item_id,
            "templateKey": template_key,
            "revision": 1,
            "sourceImageSha256": source_sha,
            "replacementStrategySha256": replacement_strategy_sha,
            "generationOptionsSha256": generation_options_sha,
            "phase": None,
            "state": rules["initialState"],
            "outcome": None,
            "history": [],
            "artifacts": {},
            "invalidationEvents": [],
            "historicalExperienceEvidence": rules[
                "historicalExperienceContract"
            ]["experienceIds"],
        }
    try:
        if not resume_visual and not resume_generation and not resume_prepared_generation:
            pin = _build_pin(rules, release)
            _atomic_write_new(output_dir / "production-pin.json", _json_bytes(pin))
            _record_artifact(manifest, output_dir, "production-pin.json", p0, [])
            evidence_source = output_dir / "evidence" / f"source-image{source_image.suffix.lower()}"
            _atomic_write_new(evidence_source, source_image.read_bytes())
            _record_artifact(manifest, output_dir, str(evidence_source.relative_to(output_dir)), p0, [])
            if preparation_stop is not None:
                raise preparation_stop
            source_analysis = (
                copy.deepcopy(prepared_source_analysis)
                if prepared_source_analysis is not None
                else _adapter_snapshot_image_object_call(
                    rules,
                    "analyze_source",
                    adapters.analyze_source,
                    source_image,
                    source_sha,
                    copy.deepcopy(replacement_strategy),
                )
            )
            if (
                source_analysis.get("sourceImageSha256") != source_sha
                or not _source_analysis_identity_valid(source_analysis, rules)
            ):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "来源分析证据与输入图片或主体身份不一致。",
                    {},
                )
            _atomic_write_new(output_dir / "source-analysis.json", _json_bytes(source_analysis))
            _record_artifact(manifest, output_dir, "source-analysis.json", p0, [str(evidence_source.relative_to(output_dir))])
            plan_dependencies = ["source-analysis.json"]
            if shared_policy_resolution is not None:
                resolution_name = rules["batchProductionContract"][
                    "resolutionArtifactName"
                ]
                _atomic_write_new(
                    output_dir / resolution_name,
                    _json_bytes(shared_policy_resolution),
                )
                _record_artifact(
                    manifest,
                    output_dir,
                    resolution_name,
                    p0,
                    ["source-analysis.json"],
                )
                plan_dependencies.append(resolution_name)
            _advance(manifest, rules, p0, timestamp)
            _persist_manifest(output_dir, manifest)

            plan = _plan_replacement(
                source_analysis,
                rules,
                template_key,
                replacement_strategy,
                shared_policy_resolution,
            )
            _atomic_write_new(output_dir / "replacement-plan.json", _json_bytes(plan))
            _record_artifact(
                manifest,
                output_dir,
                "replacement-plan.json",
                p1,
                plan_dependencies,
            )
            _advance(manifest, rules, p1, timestamp)
            _persist_manifest(output_dir, manifest)

            generation_package = _compile_generation_package(plan, source_analysis, rules)
        execution_contract = rules["generationExecutionContract"]
        task_fields = execution_contract["taskFields"]
        wal_fields = execution_contract["walFields"]
        submission_fields = execution_contract["submissionFields"]
        poll_fields = execution_contract["pollResultFields"]
        if not resume_generation:
            if not resume_prepared_generation:
                generation_package["output"]["imageCount"] = generation_options[
                    execution_contract["requestOptionFields"]["imageCount"]
                ]
                generation_package_name = _revisioned_name(
                    "generation-package.json", manifest["revision"]
                )
                _atomic_write_new(
                    output_dir / generation_package_name, _json_bytes(generation_package)
                )
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_package_name,
                    p2,
                    ["replacement-plan.json"],
                )
                generation_task_name = _revisioned_name(
                    "generation-task.json", manifest["revision"]
                )
                generation_wal_name = _revisioned_name(
                    "generation-wal.json", manifest["revision"]
                )
                generation_task_path = output_dir / generation_task_name
                generation_wal_path = output_dir / generation_wal_name
                generation_task = _compile_generation_task(
                    generation_package,
                    source_sha,
                    _sha_file(output_dir / "production-pin.json"),
                    manifest["revision"],
                    generation_options,
                    rules,
                )
                _atomic_write_new(generation_task_path, _json_bytes(generation_task))
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_task_name,
                    p2,
                    [generation_package_name, "production-pin.json"],
                )
                generation_wal = _prepared_generation_wal(
                    generation_task, timestamp, rules
                )
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
            package_request = copy.deepcopy(generation_package)
            task_request = copy.deepcopy(generation_task)
            submit_generation = getattr(adapters, "submit_generation", None)
            if not callable(submit_generation):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "生成 adapter 缺少 queued submit seam。",
                    {"operation": "submit_generation"},
                )
            try:
                generation_submission = _adapter_snapshot_image_object_call(
                    rules,
                    "submit_generation",
                    submit_generation,
                    source_image,
                    source_sha,
                    package_request,
                    task_request,
                )
            except WorkflowStop as adapter_stop:
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                generation_wal[wal_fields["failureClass"]] = execution_contract[
                    "failureClasses"
                ]["submissionUnknown"]
                generation_wal[wal_fields["failureReason"]] = adapter_stop.message
                generation_wal[wal_fields["updatedAt"]] = timestamp
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
                raise _generation_failure_stop(
                    execution_contract["failureClasses"]["submissionUnknown"],
                    adapter_stop.message,
                    rules,
                    {
                        "taskId": generation_task[task_fields["taskIdentity"]],
                        "adapterFailure": adapter_stop.evidence,
                    },
                ) from adapter_stop
            if (
                package_request != generation_package
                or task_request != generation_task
                or not _generation_submission_shape_valid(generation_submission, rules)
            ):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "生成提交结果未绑定冻结任务，或 adapter 修改了提交请求。",
                    {},
                )
            submission_status = generation_submission[submission_fields["status"]]
            if submission_status == execution_contract["submissionStatuses"]["failed"]:
                failure_class = generation_submission[
                    submission_fields["failureClass"]
                ]
                failure_reason = generation_submission[
                    submission_fields["failureReason"]
                ]
                failure_reason = _sanitize_generation_failure_reason(
                    failure_reason, rules
                )
                if failure_class == execution_contract["failureClasses"]["retryable"]:
                    failure_class = execution_contract["failureClasses"][
                        "submissionUnknown"
                    ]
                    failure_reason = "provider submission may have succeeded: " + failure_reason
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                generation_wal[wal_fields["provider"]] = generation_submission[
                    submission_fields["provider"]
                ]
                generation_wal[wal_fields["model"]] = generation_submission[
                    submission_fields["model"]
                ]
                generation_wal[wal_fields["failureClass"]] = failure_class
                generation_wal[wal_fields["failureReason"]] = failure_reason
                generation_wal[wal_fields["updatedAt"]] = timestamp
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
                raise _generation_failure_stop(
                    failure_class,
                    failure_reason,
                    rules,
                    {"taskId": generation_task[task_fields["taskIdentity"]]},
                )
            generation_wal[wal_fields["status"]] = execution_contract["walStatuses"][
                "submitted"
            ]
            for role in ("provider", "model", "providerRequestIdentity"):
                generation_wal[wal_fields[role]] = generation_submission[
                    submission_fields[role]
                ]
            generation_wal[wal_fields["failureClass"]] = None
            generation_wal[wal_fields["failureReason"]] = None
            generation_wal[wal_fields["updatedAt"]] = timestamp
            _write_generation_wal(generation_wal_path, generation_wal, rules)
            _record_artifact(
                manifest,
                output_dir,
                generation_wal_name,
                p2,
                [generation_task_name],
            )
            _persist_manifest(output_dir, manifest)
        package_request = copy.deepcopy(generation_package)
        task_request = copy.deepcopy(generation_task)
        submission_request = copy.deepcopy(generation_submission)
        if reuse_succeeded_generation:
            candidate_names = _revision_image_artifacts(
                manifest, "generated-candidate-image", manifest["revision"]
            )
            candidate_path = output_dir / candidate_names[0]
            poll_result = {
                poll_fields["status"]: execution_contract["pollStatuses"]["succeeded"],
                poll_fields["failureClass"]: None,
                poll_fields["failureReason"]: None,
                poll_fields["extension"]: candidate_path.suffix,
                poll_fields["imageBytes"]: candidate_path.read_bytes(),
                poll_fields["outputAssets"]: generation_wal[
                    wal_fields["outputAssets"]
                ],
                poll_fields["providerOutputIdentity"]: generation_wal[
                    wal_fields["providerOutputIdentity"]
                ],
            }
        else:
            retry_budget = execution_contract["retryBudgets"]["retryable"]
            if generation_wal[wal_fields["pollAttemptCount"]] >= retry_budget:
                failure_class = execution_contract["failureClasses"]["permanent"]
                failure_reason = _sanitize_generation_failure_reason(
                    "poll attempt budget exhausted before a safe retry", rules
                )
                generation_wal[wal_fields["status"]] = execution_contract[
                    "walStatuses"
                ]["failed"]
                generation_wal[wal_fields["failureClass"]] = failure_class
                generation_wal[wal_fields["failureReason"]] = failure_reason
                generation_wal[wal_fields["updatedAt"]] = timestamp
                _write_generation_wal(generation_wal_path, generation_wal, rules)
                _record_artifact(
                    manifest,
                    output_dir,
                    generation_wal_name,
                    p2,
                    [generation_task_name],
                )
                _persist_manifest(output_dir, manifest)
                raise _generation_failure_stop(
                    failure_class,
                    failure_reason,
                    rules,
                    {
                        "taskId": generation_task[task_fields["taskIdentity"]],
                        "providerRequestId": generation_submission[
                            submission_fields["providerRequestIdentity"]
                        ],
                    },
                )
            generation_wal[wal_fields["pollAttemptCount"]] += 1
            generation_wal[wal_fields["updatedAt"]] = timestamp
            _write_generation_wal(generation_wal_path, generation_wal, rules)
            _record_artifact(
                manifest,
                output_dir,
                generation_wal_name,
                p2,
                [generation_task_name],
            )
            _persist_manifest(output_dir, manifest)
            poll_generation = getattr(adapters, "poll_generation", None)
            if not callable(poll_generation):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "生成 adapter 缺少 queued poll seam。",
                    {"operation": "poll_generation"},
                )
            poll_result = _adapter_snapshot_image_object_call(
                rules,
                "poll_generation",
                poll_generation,
                source_image,
                source_sha,
                package_request,
                task_request,
                submission_request,
            )
        if (
            package_request != generation_package
            or task_request != generation_task
            or submission_request != generation_submission
            or not _generation_poll_shape_valid(poll_result, generation_task, rules)
        ):
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "生成轮询结果未绑定冻结任务、提交凭证或合法输出。",
                {},
            )
        if poll_result[poll_fields["status"]] == execution_contract["pollStatuses"][
            "failed"
        ]:
            failure_class = poll_result[poll_fields["failureClass"]]
            failure_reason = poll_result[poll_fields["failureReason"]]
            failure_reason = _sanitize_generation_failure_reason(failure_reason, rules)
            failure_role = next(
                role
                for role, value in execution_contract["failureClasses"].items()
                if value == failure_class
            )
            if generation_wal[wal_fields["pollAttemptCount"]] >= execution_contract[
                "retryBudgets"
            ][failure_role] > 0:
                failure_class = execution_contract["failureClasses"]["permanent"]
                failure_reason = "retry budget exhausted: " + failure_reason
            generation_wal[wal_fields["status"]] = execution_contract["walStatuses"][
                "failed"
            ]
            generation_wal[wal_fields["failureClass"]] = failure_class
            generation_wal[wal_fields["failureReason"]] = failure_reason
            generation_wal[wal_fields["updatedAt"]] = timestamp
            _write_generation_wal(generation_wal_path, generation_wal, rules)
            _record_artifact(
                manifest,
                output_dir,
                generation_wal_name,
                p2,
                [generation_task_name],
            )
            _persist_manifest(output_dir, manifest)
            raise _generation_failure_stop(
                failure_class,
                failure_reason,
                rules,
                {
                    "taskId": generation_task[task_fields["taskIdentity"]],
                    "providerRequestId": generation_submission[
                        submission_fields["providerRequestIdentity"]
                    ],
                },
            )
        generated_extension = poll_result[poll_fields["extension"]]
        if re.fullmatch(rules["identifiers"]["imageExtensionPattern"], generated_extension) is None:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "生成适配器返回了不安全的图片扩展名。",
                {"extension": generated_extension},
            )
        candidate_rel = _revisioned_name(
            f"evidence/generated-candidate-image{generated_extension}", manifest["revision"]
        )
        candidate_path = output_dir / candidate_rel
        _atomic_write_new(candidate_path, poll_result[poll_fields["imageBytes"]])
        _record_artifact(
            manifest,
            output_dir,
            candidate_rel,
            p2,
            [generation_package_name, generation_task_name, generation_wal_name],
        )
        generation_wal[wal_fields["status"]] = execution_contract["walStatuses"][
            "succeeded"
        ]
        generation_wal[wal_fields["providerOutputIdentity"]] = poll_result[
            poll_fields["providerOutputIdentity"]
        ]
        generation_wal[wal_fields["outputSha256"]] = _sha_file(candidate_path)
        generation_wal[wal_fields["outputAssets"]] = poll_result[
            poll_fields["outputAssets"]
        ]
        generation_wal[wal_fields["failureClass"]] = None
        generation_wal[wal_fields["failureReason"]] = None
        generation_wal[wal_fields["updatedAt"]] = timestamp
        _write_generation_wal(generation_wal_path, generation_wal, rules)
        _record_artifact(
            manifest,
            output_dir,
            generation_wal_name,
            p2,
            [generation_task_name],
        )
        _persist_manifest(output_dir, manifest)
        review_bindings = {
            "generatedImageSha256": _sha_file(candidate_path),
            "generationPackageSha256": _sha_bytes(_canonical_bytes(generation_package)),
        }
        operation_request_field = rules["multiInstanceContract"]["generationFields"][
            "imageOperations"
        ]
        review_request = {
            "bindings": copy.deepcopy(review_bindings),
            operation_request_field: copy.deepcopy(
                generation_package[operation_request_field]
            ),
        }
        review_request_snapshot = copy.deepcopy(review_request)
        review = _adapter_snapshot_image_object_call(
            rules,
            "inspect_generated",
            adapters.inspect_generated,
            candidate_path,
            review_bindings["generatedImageSha256"],
            review_request,
        )
        candidate_unchanged = _sha_file(candidate_path) == review_bindings["generatedImageSha256"]
        review_request_unchanged = review_request == review_request_snapshot
        gate_stop = _evaluate_visual_gate(
            review,
            rules,
            review_bindings,
            identity_text_required=(
                rules["identityReplacementContract"]["planFields"]["route"] in plan
            ),
            expected_image_operations=plan[
                rules["multiInstanceContract"]["planFields"]["imageOperations"]
            ],
        )
        if not candidate_unchanged or not review_request_unchanged:
            if isinstance(review, dict):
                review["decision"] = rules["visualReviewContract"]["decisionValues"]["rejected"]
                review["decisionEvidence"] = {
                    "candidateBytesUnchanged": candidate_unchanged,
                    "reviewRequestUnchanged": review_request_unchanged,
                }
            gate_stop = _stop(
                rules,
                "failed",
                "externalFailure",
                "视觉审核期间候选图或审核请求发生变化，审核绑定已失效。",
                {"path": candidate_rel},
            )
        review_name = _revisioned_name("visual-review.json", manifest["revision"])
        _atomic_write_new(output_dir / review_name, _json_bytes(review))
        _record_artifact(manifest, output_dir, review_name, p2, [candidate_rel, generation_package_name])
        if gate_stop is not None:
            raise gate_stop
        approved_rel = _revisioned_name(
            f"evidence/approved-template-image{generated_extension}", manifest["revision"]
        )
        approved_path = output_dir / approved_rel
        _atomic_write_new(approved_path, candidate_path.read_bytes())
        _record_artifact(manifest, output_dir, approved_rel, p2, [candidate_rel, review_name])
        _advance(manifest, rules, p2, timestamp)
        _persist_manifest(output_dir, manifest)

        approved_sha = _sha_file(approved_path)
        analysis = _adapter_snapshot_image_object_call(
            rules,
            "analyze_approved",
            adapters.analyze_approved,
            approved_path,
            approved_sha,
        )
        if analysis.get("visualFactSourceSha256") != approved_sha:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "模板分析修改了确认模板图或未绑定视觉审核通过的图片摘要。",
                {"approvedImageSha256": approved_sha},
            )
        _atomic_write_new(output_dir / "template-analysis.json", _json_bytes(analysis))
        _record_artifact(manifest, output_dir, "template-analysis.json", p3, [approved_rel, review_name])
        _advance(manifest, rules, p3, timestamp)
        _persist_manifest(output_dir, manifest)

        editable = _compile_editable_spec(analysis, rules, plan)
        _atomic_write_new(output_dir / "editable-template-spec.json", _json_bytes(editable))
        _record_artifact(manifest, output_dir, "editable-template-spec.json", p4, ["template-analysis.json"])
        _advance(manifest, rules, p4, timestamp)
        _persist_manifest(output_dir, manifest)

        hidden = _compile_hidden_spec(analysis, editable, rules)
        _atomic_write_new(output_dir / "hidden-template-spec.json", _json_bytes(hidden))
        _record_artifact(manifest, output_dir, "hidden-template-spec.json", p5, ["template-analysis.json", "editable-template-spec.json"])
        draft = _compile_draft(
            template_key,
            source_analysis.get("imageSize", "1024x1024"),
            editable,
            hidden,
            rules,
        )
        _atomic_write_new(output_dir / "gallery-template.draft.json", _json_bytes(draft))
        _record_artifact(manifest, output_dir, "gallery-template.draft.json", p5, ["editable-template-spec.json", "hidden-template-spec.json"])
        _advance(manifest, rules, p5, timestamp)
        _persist_manifest(output_dir, manifest)

        semantic_audit_content = _semantic_audit_payload(draft, editable, rules)
        compiled_content_sha = _sha_bytes(_canonical_bytes(semantic_audit_content))
        semantic_audit_request = copy.deepcopy(semantic_audit_content)
        audit_request_sha = _sha_bytes(_canonical_bytes(semantic_audit_request))
        semantic_audit = _adapter_object_call(
            rules,
            "audit_semantics",
            adapters.audit_semantics,
            semantic_audit_request,
        )
        compiled_content_unchanged = (
            _sha_bytes(_canonical_bytes(_semantic_audit_payload(draft, editable, rules)))
            == compiled_content_sha
        )
        audit_request_unchanged = (
            _sha_bytes(_canonical_bytes(semantic_audit_request)) == audit_request_sha
        )
        if not compiled_content_unchanged or not audit_request_unchanged:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "语义审计 adapter 修改了只读编译快照。",
                {
                    "compiledContentUnchanged": compiled_content_unchanged,
                    "auditRequestUnchanged": audit_request_unchanged,
                },
            )
        _atomic_write_new(output_dir / "semantic-audit.json", _json_bytes(semantic_audit))
        _record_artifact(
            manifest,
            output_dir,
            "semantic-audit.json",
            p6,
            ["gallery-template.draft.json", "editable-template-spec.json"],
        )
        validation = _validation_report(
            draft,
            editable,
            plan,
            source_analysis,
            review,
            semantic_audit,
            rules,
        )
        _atomic_write_new(output_dir / "validation-report.json", _json_bytes(validation))
        _record_artifact(
            manifest,
            output_dir,
            "validation-report.json",
            p6,
            ["gallery-template.draft.json", review_name, "semantic-audit.json"],
        )
        if not validation["pass"]:
            raise _stop(rules, "blocked", "contractFailure", "四层静态验收未通过。", validation)
        _advance(manifest, rules, p6, timestamp)
        _persist_manifest(output_dir, manifest)

        delivery_errors, delivery = _delivery_image_context(output_dir, manifest)
        if delivery_errors:
            raise _stop(
                rules,
                "blocked",
                "productionItemIntegrityFailure",
                "上传前候选图与确认模板图谱系不一致。",
                {"errors": delivery_errors},
            )
        object_key = _object_storage_key(
            template_key,
            delivery["approvedPath"],
            delivery["approvedSha256"],
            rules,
        )
        receipt_path = output_dir / "asset-receipt.json"
        if receipt_path.exists():
            receipt = _load_json(receipt_path)
            if not _asset_receipt_valid(
                receipt, manifest, delivery, object_key, rules
            ):
                raise _stop(
                    rules,
                    "blocked",
                    "productionItemIntegrityFailure",
                    "已有 Asset Receipt 与当前确认模板图或对象键不一致。",
                    {"path": str(receipt_path)},
                )
        else:
            upload_result = _adapter_snapshot_image_object_call(
                rules,
                "upload",
                adapters.upload,
                delivery["approvedPath"],
                delivery["approvedSha256"],
                object_key,
            )
            if not _upload_result_valid(
                upload_result, object_key, delivery["approvedSha256"], rules
            ):
                raise _stop(
                    rules,
                    "failed",
                    "externalFailure",
                    "上传结果未绑定确认模板图、远端对象或请求身份。",
                    {},
                )
            receipt = _build_asset_receipt(
                manifest, delivery, upload_result, rules
            )
            _atomic_write_new(receipt_path, _json_bytes(receipt))
        _record_artifact(
            manifest,
            output_dir,
            "asset-receipt.json",
            p7,
            [delivery["approvedName"], "validation-report.json"],
        )
        _advance(manifest, rules, p7, timestamp)
        _persist_manifest(output_dir, manifest)

        receipt_fields = rules["objectStorageContract"]["receiptFields"]
        final_record = _formal_projection(
            draft, receipt[receipt_fields["url"]], rules
        )
        final_validation = _validate_final(final_record, rules)
        _atomic_write_new(output_dir / "final-validation-report.json", _json_bytes(final_validation))
        _record_artifact(manifest, output_dir, "final-validation-report.json", p8, ["gallery-template.draft.json", "asset-receipt.json"])
        if not final_validation["pass"]:
            raise _stop(rules, "blocked", "contractFailure", "正式 JSON 最终合同验证未通过。", final_validation)
        _atomic_write_new(output_dir / "gallery-template.json", _json_bytes(final_record))
        _record_artifact(manifest, output_dir, "gallery-template.json", p8, ["gallery-template.draft.json", "asset-receipt.json", "final-validation-report.json"])
        _advance(manifest, rules, p8, timestamp)
        manifest["outcome"] = "completed"
        _persist_manifest(output_dir, manifest)
        return ProductionResult(
            "completed",
            item_id,
            rules["resultStates"]["completed"],
            output_dir,
            output_dir / "gallery-template.json",
            resumed=resumed,
        )
    except WorkflowStop as stop:
        manifest["state"] = stop.state
        manifest["outcome"] = stop.outcome
        manifest["error"] = {"code": stop.error_code, "message": stop.message, "evidence": stop.evidence}
        _persist_manifest(output_dir, manifest)
        return ProductionResult(
            stop.outcome,
            item_id,
            stop.state,
            output_dir,
            error_code=stop.error_code,
            message=stop.message,
            resumed=resumed,
        )


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
