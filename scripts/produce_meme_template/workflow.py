from __future__ import annotations

import copy
import hashlib
import ipaddress
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
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "contracts" / "machine-rules.json"
GALLERY_SCHEMA_PATH = REPO_ROOT / "contracts" / "gallery-template.schema.json"
RELEASE_PATH = REPO_ROOT / "release.json"
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
VISIBLE_TEXT_LEXEME = re.compile(
    r"[A-Za-z]+(?:['’][A-Za-z]+)*|\d+|[\u3400-\u4dbf\u4e00-\u9fff]{2,}"
)
GALLERY_SCHEMA = json.loads(GALLERY_SCHEMA_PATH.read_text(encoding="utf-8"))
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
    def generate(self, source_image: Path, generation_package: dict[str, Any]) -> dict[str, Any]: ...
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


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


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


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


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


def _is_valid_https_url(value: Any) -> bool:
    if not isinstance(value, str) or any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    host_valid = False
    if hostname:
        try:
            ipaddress.ip_address(hostname)
            host_valid = True
        except ValueError:
            try:
                ascii_hostname = hostname.encode("idna").decode("ascii").removesuffix(".")
            except UnicodeError:
                ascii_hostname = ""
            host_valid = bool(
                ascii_hostname
                and len(ascii_hostname) <= 253
                and all(
                    len(label) <= 63
                    and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                    for label in ascii_hostname.split(".")
                )
            )
    return bool(
        parsed.scheme == "https"
        and host_valid
        and parsed.netloc
        and port != 0
        and parsed.username is None
        and parsed.password is None
    )


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
            and _is_valid_https_url(value)
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
    required_artifacts: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    expected_identity = {
        "productionItemId": production_item_id,
        "templateKey": template_key,
        "sourceImageSha256": source_sha256,
        "replacementStrategySha256": replacement_strategy_sha256,
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
        dependencies = artifact.get("dependsOn")
        if not isinstance(dependencies, list) or not all(
            isinstance(dependency, str) and dependency for dependency in dependencies
        ):
            errors.append(f"{name} dependencies invalid")
        else:
            for dependency in dependencies:
                if dependency not in artifacts:
                    errors.append(f"{name} dependency missing: {dependency}")
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
        if _sha_file(artifact_path) != artifact.get("sha256"):
            errors.append(f"{name} digest mismatch")
    return errors


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
        r"^(?:generation-package|visual-review|evidence/(?:generated-candidate-image|approved-template-image))\.[a-z0-9]+$"
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
    manifest["artifacts"][name] = {
        "path": name,
        "sha256": _sha_file(path),
        "bytes": path.stat().st_size,
        "phase": phase,
        "revision": manifest["revision"],
        "dependsOn": dependencies,
    }


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
            {"operation": operation, "exceptionType": type(exc).__name__, "detail": str(exc)},
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


def _normalize_replacement_strategy(request: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any] | None:
    strategy = request.get("replacementStrategy")
    if strategy is None:
        return None
    normalized = dict(strategy)
    for field in rules["replacementStrategyContract"]["listFields"]:
        if field in normalized:
            normalized[field] = sorted(normalized[field])
    return normalized


def _build_pin(rules: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    contract_bytes = GALLERY_SCHEMA_PATH.read_bytes()
    skill_manifest_path = REPO_ROOT / "skill-manifest.json"
    skill_manifest = _load_json(skill_manifest_path)
    release_files = []
    for relative in skill_manifest["tracked_files"]:
        tracked_path = REPO_ROOT / relative
        if not tracked_path.is_file():
            raise FileNotFoundError(f"发布 manifest 中的文件不存在：{relative}")
        release_files.append({"path": relative, "sha256": _sha_file(tracked_path)})
    release_digest = _sha_bytes(_canonical_bytes({"files": release_files}))
    return {
        "artifactType": "production-pin",
        "schemaVersion": rules["schemaVersion"],
        "skill": {"name": release["skillName"], "version": release["skillVersion"]},
        "artifactSchemaVersion": release["artifactSchemaVersion"],
        "releaseSha256": release_digest,
        "releaseManifestSha256": _sha_file(skill_manifest_path),
        "releaseFileCount": len(release_files),
        "machineRulesSha256": _sha_file(RULES_PATH),
        "galleryContract": {
            "id": release["supportedContracts"]["galleryTemplate"],
            "snapshot": "contracts/gallery-template.schema.json",
            "sha256": _sha_bytes(contract_bytes),
            "upstreamSourceSha256": "1ebe5cb0790fa20e5968570c7b09d83d7c14b9347bcf5e60ca612384a3a81619",
        },
    }


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
        strategy = {
            "source": per_image_source,
            "decisionSource": per_image_source,
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
        decision_source = per_image_source
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
    frozen_decision_sources.update({value: per_image_source for value in preserve_values})
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
        and _is_valid_https_url(url)
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
        _is_valid_https_url(cover)
        and _is_valid_https_url(reference_image)
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


def _finalize_uploaded_item(
    output_dir: Path,
    manifest: dict[str, Any],
    rules: dict[str, Any],
    timestamp: str,
) -> ProductionResult:
    draft = _load_json(output_dir / "gallery-template.draft.json")
    receipt = _load_json(output_dir / "asset-receipt.json")
    approved_names = _revision_image_artifacts(
        manifest,
        "approved-template-image",
        manifest["revision"],
    )
    if len(approved_names) != 1:
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 恢复要求唯一的确认模板图谱系。",
            {"approvedArtifacts": approved_names},
        )
    approved_name = approved_names[0]
    approved_path = output_dir / approved_name
    expected_object_key = (
        f"gallery/templates/{manifest['templateKey']}/{_sha_file(approved_path)}{approved_path.suffix.lower()}"
    )
    receipt_valid = (
        receipt.get("artifactType") == "asset-receipt"
        and receipt.get("schemaVersion") == rules["schemaVersion"]
        and receipt.get("imageSha256") == _sha_file(approved_path)
        and receipt.get("objectKey") == expected_object_key
        and _is_valid_https_url(receipt.get("url"))
    )
    if not receipt_valid:
        raise _stop(
            rules,
            "blocked",
            "productionItemIntegrityFailure",
            "P7 Asset Receipt 与确认模板图或对象键不一致。",
            {"path": str(output_dir / "asset-receipt.json")},
        )

    final_record = _formal_projection(draft, receipt["url"], rules)
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


def run_production(
    request: dict[str, Any],
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ProductionResult:
    """Run one independent Production Item through P0-P8.

    The request accepts one source image and optional per-image replacementStrategy.
    Uncovered decisions use the default route, and no state is shared across Production Items.
    Shared batch strategy is reserved for a later ticket.
    """

    rules = _load_json(RULES_PATH)
    release = _load_json(RELEASE_PATH)
    p0, p1, p2, p3, p4, p5, p6, p7, p8 = (item["phase"] for item in rules["productionPhases"])
    now = clock or (lambda: datetime.now(timezone.utc))
    timestamp = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    output_root_path = Path(output_root).resolve()
    schema = _load_json(GALLERY_SCHEMA_PATH)
    template_key = str(request.get("templateKey", ""))
    production_item_id = request.get("productionItemId")
    key_pattern = schema["properties"]["key"]["pattern"]
    item_pattern = rules["identifiers"]["productionItemIdPattern"]
    invalid_identifiers = []
    if re.fullmatch(key_pattern, template_key) is None:
        invalid_identifiers.append("templateKey")
    if production_item_id is not None and re.fullmatch(item_pattern, str(production_item_id)) is None:
        invalid_identifiers.append("productionItemId")
    strategy_errors = _replacement_strategy_errors(request, rules)
    if invalid_identifiers or strategy_errors:
        details = [*(f"非法标识符：{field}" for field in invalid_identifiers), *strategy_errors]
        return ProductionResult(
            "needs_input",
            str(production_item_id or "invalid-production-item"),
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="生产请求预检失败：" + "；".join(details),
        )
    replacement_strategy = _normalize_replacement_strategy(request, rules)
    source_image = Path(request["sourceImage"]).resolve()
    if not source_image.is_file():
        raise FileNotFoundError(source_image)
    source_sha = _sha_file(source_image)
    replacement_strategy_sha = _sha_bytes(_canonical_bytes(replacement_strategy))
    item_id = str(production_item_id or f"{template_key}-{source_sha[:12]}")
    output_dir = (output_root_path / item_id).resolve()
    if not output_dir.is_relative_to(output_root_path) or output_dir.parent != output_root_path:
        return ProductionResult(
            "needs_input",
            item_id,
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="Production Item 输出目录越出 output root。",
        )
    manifest_path = output_dir / "production-manifest.json"
    resume_visual = False
    resumed = False
    source_analysis: dict[str, Any]
    plan: dict[str, Any]
    generation_package: dict[str, Any]
    if manifest_path.exists():
        existing = _load_json(manifest_path)
        completed_artifacts = ("production-pin.json", "gallery-template.json", "final-validation-report.json")
        identity_errors = _production_item_integrity_errors(
            output_dir,
            existing,
            production_item_id=item_id,
            template_key=template_key,
            source_sha256=source_sha,
            replacement_strategy_sha256=replacement_strategy_sha,
            required_artifacts=completed_artifacts if existing.get("state") == rules["resultStates"]["completed"] else (),
        )
        if existing.get("state") == rules["resultStates"]["completed"]:
            identity_errors.extend(_current_p2_artifact_errors(existing))
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
                required_artifacts=(
                    "production-pin.json",
                    "gallery-template.draft.json",
                    "validation-report.json",
                    "asset-receipt.json",
                ),
            )
            recovery_errors.extend(_current_p2_artifact_errors(existing))
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
        if existing.get("error", {}).get("code") == rules["errorCodes"]["visualHardFailure"]:
            previous_revision = existing.get("revision")
            if not isinstance(previous_revision, int) or previous_revision < 1:
                recovery_errors = ["manifest revision invalid"]
            else:
                previous_package_name = _revisioned_name("generation-package.json", previous_revision)
                previous_review_name = _revisioned_name("visual-review.json", previous_revision)
                recovery_errors = _production_item_integrity_errors(
                    output_dir,
                    existing,
                    production_item_id=item_id,
                    template_key=template_key,
                    source_sha256=source_sha,
                    replacement_strategy_sha256=replacement_strategy_sha,
                    required_artifacts=(
                        "production-pin.json",
                        "source-analysis.json",
                        "replacement-plan.json",
                        previous_package_name,
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
    if not resume_visual:
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "artifactType": "production-manifest",
            "schemaVersion": rules["schemaVersion"],
            "productionItemId": item_id,
            "templateKey": template_key,
            "revision": 1,
            "sourceImageSha256": source_sha,
            "replacementStrategySha256": replacement_strategy_sha,
            "phase": None,
            "state": rules["initialState"],
            "outcome": None,
            "history": [],
            "artifacts": {},
            "invalidationEvents": [],
            "historicalExperienceEvidence": rules["historicalExperienceEvidence"],
        }
    try:
        if not resume_visual:
            pin = _build_pin(rules, release)
            _atomic_write_new(output_dir / "production-pin.json", _json_bytes(pin))
            _record_artifact(manifest, output_dir, "production-pin.json", p0, [])
            evidence_source = output_dir / "evidence" / f"source-image{source_image.suffix.lower()}"
            _atomic_write_new(evidence_source, source_image.read_bytes())
            _record_artifact(manifest, output_dir, str(evidence_source.relative_to(output_dir)), p0, [])
            source_analysis = _adapter_snapshot_image_object_call(
                rules,
                "analyze_source",
                adapters.analyze_source,
                source_image,
                source_sha,
                copy.deepcopy(replacement_strategy),
            )
            if source_analysis.get("sourceImageSha256") != source_sha:
                raise _stop(rules, "failed", "externalFailure", "来源分析证据与输入图片 SHA 不一致。", {})
            _atomic_write_new(output_dir / "source-analysis.json", _json_bytes(source_analysis))
            _record_artifact(manifest, output_dir, "source-analysis.json", p0, [str(evidence_source.relative_to(output_dir))])
            _advance(manifest, rules, p0, timestamp)
            _persist_manifest(output_dir, manifest)

            plan = _plan_replacement(source_analysis, rules, template_key, replacement_strategy)
            _atomic_write_new(output_dir / "replacement-plan.json", _json_bytes(plan))
            _record_artifact(manifest, output_dir, "replacement-plan.json", p1, ["source-analysis.json"])
            _advance(manifest, rules, p1, timestamp)
            _persist_manifest(output_dir, manifest)

            generation_package = _compile_generation_package(plan, source_analysis, rules)
        generation_package_name = _revisioned_name("generation-package.json", manifest["revision"])
        _atomic_write_new(output_dir / generation_package_name, _json_bytes(generation_package))
        _record_artifact(manifest, output_dir, generation_package_name, p2, ["replacement-plan.json"])
        generation_request = copy.deepcopy(generation_package)
        generated = _adapter_snapshot_image_object_call(
            rules,
            "generate",
            adapters.generate,
            source_image,
            source_sha,
            generation_request,
        )
        generated_contract_valid = bool(
            isinstance(generated, dict)
            and generation_request == generation_package
            and generated.get("requestId") == generation_package["requestId"]
            and isinstance(generated.get("imageBytes"), bytes)
        )
        if not generated_contract_valid:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "生成适配器结果未绑定当前 Generation Package request ID 或图片字节。",
                {},
            )
        generated_extension = str(generated.get("extension", ""))
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
        _atomic_write_new(candidate_path, generated["imageBytes"])
        _record_artifact(manifest, output_dir, candidate_rel, p2, [generation_package_name])
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

        object_key = f"gallery/templates/{template_key}/{_sha_file(approved_path)}{approved_path.suffix.lower()}"
        if ".." in object_key or object_key.startswith("/"):
            raise _stop(rules, "blocked", "contractFailure", "OSS 对象键不安全。", {"objectKey": object_key})
        receipt_path = output_dir / "asset-receipt.json"
        if receipt_path.exists():
            receipt = _load_json(receipt_path)
            receipt_valid = (
                receipt.get("artifactType") == "asset-receipt"
                and receipt.get("schemaVersion") == rules["schemaVersion"]
                and receipt.get("imageSha256") == _sha_file(approved_path)
                and receipt.get("objectKey") == object_key
                and _is_valid_https_url(receipt.get("url"))
            )
            if not receipt_valid:
                raise _stop(
                    rules,
                    "blocked",
                    "productionItemIntegrityFailure",
                    "已有 Asset Receipt 与当前确认模板图或对象键不一致。",
                    {"path": str(receipt_path)},
                )
        else:
            approved_sha = _sha_file(approved_path)
            receipt = _adapter_snapshot_image_object_call(
                rules,
                "upload",
                adapters.upload,
                approved_path,
                approved_sha,
                object_key,
            )
            if receipt.get("imageSha256") != approved_sha or not _is_valid_https_url(receipt.get("url")):
                raise _stop(rules, "failed", "externalFailure", "上传凭证与确认模板图不一致。", receipt)
            receipt = {"artifactType": "asset-receipt", "schemaVersion": rules["schemaVersion"], **receipt}
            _atomic_write_new(receipt_path, _json_bytes(receipt))
        _record_artifact(manifest, output_dir, "asset-receipt.json", p7, [approved_rel, "validation-report.json"])
        _advance(manifest, rules, p7, timestamp)
        _persist_manifest(output_dir, manifest)

        final_record = _formal_projection(draft, receipt["url"], rules)
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
