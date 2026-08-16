from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "contracts" / "machine-rules.json"
GALLERY_SCHEMA_PATH = REPO_ROOT / "contracts" / "gallery-template.schema.json"
RELEASE_PATH = REPO_ROOT / "release.json"
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
INPUT_ID_PATTERN = json.loads(GALLERY_SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]["inputId"]["pattern"]
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
        self, generated_image: Path, review_context: dict[str, str]
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


def _deep_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_deep_keys(v) for v in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_deep_keys(v) for v in value), set())
    return set()


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
    closure = source_analysis.get("dependencyClosure", [])
    if not isinstance(closure, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("type"), str)
        and item.get("type").strip()
        and isinstance(item.get("value"), str)
        and item.get("value").strip()
        for item in closure
    ):
        raise _stop(
            rules,
            "failed",
            "externalFailure",
            "来源分析的 dependencyClosure 必须是包含非空 type/value 的对象列表。",
            {"actualType": type(closure).__name__},
        )
    if not closure:
        raise _stop(
            rules,
            "needs_input",
            "riskNeedsReview",
            "主要替换目标的依赖范围尚无法可靠判定，需要复核。",
            {"category": category},
        )
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
    changed_components = {
        "primary-role": source_analysis["target"]["role"],
        "primary-identity": source_analysis["target"]["identity"],
        **{
            f"dependency-{index}-{item['type']}": item["value"]
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
    return {
        "artifactType": "replacement-plan",
        "schemaVersion": rules["schemaVersion"],
        "templateKey": template_key,
        "strategy": strategy,
        "mechanism": source_analysis["mechanism"],
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
                    "value": item["value"],
                    "dependencyType": item["type"],
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
    }


def _compile_generation_package(plan: dict[str, Any], source_analysis: dict[str, Any]) -> dict[str, Any]:
    target = plan["primaryTargets"][0]
    sections = {
        "task": "基于参考资产完成整图重构，输出一张可独立使用的新模板图。",
        "replacementTarget": f"将{target['sourceRole']}完整替换为{target['replacementValue']}。",
        "dependencyClosure": "；".join(item["value"] for item in plan["dependencyClosure"]),
        "frozenSet": "；".join(plan["frozenSet"]),
        "mediumContract": "；".join(f"{key}: {value}" for key, value in source_analysis["visualContract"].items()),
        "residueCleanup": "清理旧身份特征、旧轮廓、水印、签名、平台标和账户标。",
        "spatialRelations": "；".join(source_analysis["spatialRelations"]),
        "output": "保持完整画布与原比例，清晰输出，不新增文字。",
    }
    request_id = "gen-" + _sha_bytes(_canonical_bytes({"plan": plan, "sections": sections}))[:24]
    return {
        "artifactType": "generation-package",
        "schemaVersion": plan["schemaVersion"],
        "requestId": request_id,
        "sections": sections,
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
    evidence_fields = contract["evidenceFields"]
    hard_gates = review.get("hardGates")
    dimensions = review.get("visualDimensions")
    visible_text = review.get("visibleTextEvidence")
    cleanliness = review.get("cleanlinessFindings")
    ambiguities = review.get("ambiguitySignals")
    bindings = review.get("bindings")
    method = review.get("method")
    evidence_payload = (
        {field: review[field] for field in evidence_fields}
        if all(field in review for field in evidence_fields)
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


def _compile_editable_spec(analysis: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    slot_contract = rules["slotCompilationContract"]
    value_gate_roles = tuple(slot_contract["valueGateRoles"].values())
    slot_candidates = analysis.get("slotCandidates")
    slot_candidates_valid = bool(
        isinstance(slot_candidates, list)
        and all(
            isinstance(slot, dict)
            and isinstance(slot.get("id"), str)
            and SLOT_ID.fullmatch(slot["id"])
            and isinstance(slot.get("semanticRole"), str)
            and slot["semanticRole"].strip()
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
            "槽位候选必须为四道具名价值门禁提供完整布尔结论。",
            {},
        )
    slots = [slot for slot in slot_candidates if all(slot["valueGates"][role] for role in value_gate_roles)]
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
    subject_role = slot_contract["semanticRoles"]["primarySubject"]
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
                and (
                    not assessment["includedAsSlot"]
                    or any(slot.get("semanticRole") == role for slot in slots)
                )
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
    count_fields = set(slot_contract["assetUnitCountFields"].values())
    control_count_field = slot_contract["assetUnitCountFields"]["controls"]
    asset_units_valid = bool(
        isinstance(asset_units, dict)
        and set(asset_units) == {*count_fields, "evidence"}
        and all(
            isinstance(asset_units[field], int)
            and not isinstance(asset_units[field], bool)
            and asset_units[field] >= 0
            for field in count_fields
        )
        and asset_units[control_count_field] == len(slots)
        and isinstance(asset_units.get("evidence"), str)
        and asset_units["evidence"].strip()
    )
    if not asset_units_valid:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "可见主体、身份、上传素材和控件数量缺少独立计数证据，或控件数与槽位不一致。",
            {},
        )
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
    if preference_exceptions:
        editable["defaultValuePreferenceExceptionEvidence"] = preference_exceptions
    return editable


def _slot_to_input(slot: dict[str, Any]) -> dict[str, Any]:
    if slot["type"] == "subject":
        return {
            "id": slot["id"],
            "type": "subject",
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
                "promptValue": "用户上传图中的主体",
                "hint": "上传1张清晰主体图，按模板参考图的媒介与区域职责完整重绘",
                "extract": "提取该主体可辨识的身份特征，并在模板参考图的媒介与造型体系中重绘。",
                "maxCount": 1,
                "minWidth": 256,
                "minHeight": 256,
                "private": True,
                "sourceOptions": ["upload", "recent_upload", "asset_library"],
            },
        }
    return {
        "id": slot["id"],
        "type": "prompt",
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
        "inputSchema": [_slot_to_input(slot) for slot in editable["slots"]],
        "promptEnhancement": {
            "stageKey": "gallery.prompt_rewrite",
            "instruction": instruction,
            "referenceField": "referenceImage",
            "lockedConstraints": locked_constraints,
            "preserve": preserve,
            "output": {"format": "json", "promptField": "finalPrompt"},
        },
    }


def _compile_draft(template_key: str, image_size: str, editable: dict[str, Any], hidden: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": template_key,
        "status": "DRAFT",
        "title": editable["title"],
        "description": editable["description"],
        "imageSize": image_size,
        "promptTemplate": editable["promptTemplate"],
        "inputSchema": hidden["inputSchema"],
        "promptEnhancement": hidden["promptEnhancement"],
        "metadata": {"tags": editable["tags"]},
    }


SENTENCE_PUNCTUATION = re.compile(r"[，。！？；,.!?;]")


def _resolve_prompt(prompt_template: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), prompt_template)


def _semantic_audit_payload(draft: dict[str, Any], editable: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy({
        "title": draft["title"],
        "promptTemplate": draft["promptTemplate"],
        "promptEnhancement": draft["promptEnhancement"],
        "freeEditableContent": editable["freeEditableContent"],
        "slots": [
            {
                "id": slot["id"],
                "semanticRole": slot["semanticRole"],
                "defaultValue": slot["defaultValue"],
                "suggestions": slot["suggestions"],
            }
            for slot in editable["slots"]
        ],
    })


def _validation_report(
    draft: dict[str, Any],
    editable: dict[str, Any],
    source_analysis: dict[str, Any],
    review: dict[str, Any],
    semantic_audit: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    schema = _load_json(GALLERY_SCHEMA_PATH)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(draft), key=lambda item: list(item.path)
    )
    input_ids = {item["id"] for item in draft["inputSchema"]}
    referenced_ids = set(PLACEHOLDER.findall(draft["promptTemplate"]))
    missing_placeholders = sorted(input_ids - referenced_ids)
    unknown_placeholders = sorted(referenced_ids - input_ids)
    missing_free_editable_content = sorted(
        value for value in editable.get("freeEditableContent", []) if value not in draft["promptTemplate"]
    )
    default_values = {slot["id"]: slot["defaultValue"] for slot in editable["slots"]}
    resolved_prompts = [("defaults", _resolve_prompt(draft["promptTemplate"], default_values))]
    for slot in editable["slots"]:
        for suggestion in slot.get("suggestions", []):
            scenario_values = {**default_values, slot["id"]: suggestion}
            resolved_prompts.append(
                (f"{slot['id']}={suggestion}", _resolve_prompt(draft["promptTemplate"], scenario_values))
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
    forbidden_keys = sorted(_deep_keys(draft) & set(rules["formalProjection"]["forbiddenKeys"]))
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
    hidden_text = " ".join(_deep_strings(draft["promptEnhancement"]))
    open_content_conflicts = sorted(
        value for value in slot_values | free_editable_values if value in hidden_text
    )
    open_axis_conflicts = sorted(
        token
        for slot in editable["slots"]
        for token in slot.get("hiddenConflictTokens", [])
        if token in hidden_text
    )
    title_slot_leaks = sorted(value for value in slot_values if value in draft["title"])
    title_forbidden_tokens = sorted(
        token
        for slot in editable["slots"]
        for token in slot.get("titleForbiddenTokens", [])
        if token in draft["title"]
    )
    audited_content_sha = _sha_bytes(_canonical_bytes(_semantic_audit_payload(draft, editable)))
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
    resolved_cases = evidence.get(resolved_cases_field)
    reviewed_open_axes = evidence.get(open_axes_field)
    maximum_difference_inputs = evidence.get(maximum_difference_field)
    suggestion_reviews = evidence.get(suggestion_reviews_field)
    instruction_scope_review = evidence.get(instruction_scope_field)
    hidden_responsibility_review = evidence.get(hidden_responsibility_field)
    expected_resolved_cases = {label for label, _ in resolved_prompts}
    expected_open_axes = {slot["semanticRole"] for slot in editable["slots"]}
    expected_slot_ids = {slot["id"] for slot in editable["slots"]}
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
            and semantic_audit_passed,
            "evidence": {
                "sourceLeaks": source_leaks,
                "missingSlotBindings": missing_placeholders,
                "unknownSlotBindings": unknown_placeholders,
                "missingFreeEditableContent": missing_free_editable_content,
                "unnaturalResolvedPrompts": unnatural_resolved_prompts,
                "titleSlotLeaks": title_slot_leaks,
                "titleForbiddenTokens": title_forbidden_tokens,
                "semanticAudit": {
                    "contractValid": semantic_audit_contract_valid,
                    "contentBound": semantic_audit_bound,
                    "checks": semantic_audit_checks,
                    "evidence": semantic_audit.get("evidence", {}),
                },
            },
        },
        "visualContract": {
            "pass": all(review["hardGates"].values())
            and all(item["pass"] for item in review["visualDimensions"].values()),
            "evidence": {"reviewSha256": _sha_bytes(_json_bytes(review))},
        },
        "galleryContract": {
            "pass": not forbidden_keys
            and not production_terms
            and not open_content_conflicts
            and not open_axis_conflicts,
            "evidence": {
                "forbiddenKeys": forbidden_keys,
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
    complete = dict(draft)
    complete["cover"] = url
    complete["referenceImage"] = url
    projection = {key: complete[key] for key in rules["formalProjection"]["topLevel"] if key in complete}
    projection["metadata"] = {
        key: complete["metadata"][key]
        for key in rules["formalProjection"]["metadata"]
        if key in complete["metadata"]
    }
    return projection


def _validate_final(record: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    schema = _load_json(GALLERY_SCHEMA_PATH)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(record), key=lambda item: list(item.path)
    )
    forbidden_keys = sorted(_deep_keys(record) & set(rules["formalProjection"]["forbiddenKeys"]))
    top_level_extra = sorted(set(record) - set(rules["formalProjection"]["topLevel"]))
    passed = not errors and not forbidden_keys and not top_level_extra and record.get("cover") == record.get("referenceImage")
    return {
        "artifactType": "final-validation-report",
        "schemaVersion": rules["schemaVersion"],
        "pass": passed,
        "schemaErrors": [error.message for error in errors],
        "forbiddenKeys": forbidden_keys,
        "topLevelExtra": top_level_extra,
        "coverMatchesReferenceImage": record.get("cover") == record.get("referenceImage"),
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
        and str(receipt.get("url", "")).startswith("https://")
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
            source_analysis = _adapter_call(
                rules,
                "analyze_source",
                adapters.analyze_source,
                source_image,
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

            generation_package = _compile_generation_package(plan, source_analysis)
        generation_package_name = _revisioned_name("generation-package.json", manifest["revision"])
        _atomic_write_new(output_dir / generation_package_name, _json_bytes(generation_package))
        _record_artifact(manifest, output_dir, generation_package_name, p2, ["replacement-plan.json"])
        generation_request = copy.deepcopy(generation_package)
        generated = _adapter_call(
            rules,
            "generate",
            adapters.generate,
            source_image,
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
        review = _adapter_call(
            rules,
            "inspect_generated",
            adapters.inspect_generated,
            candidate_path,
            copy.deepcopy(review_bindings),
        )
        candidate_unchanged = _sha_file(candidate_path) == review_bindings["generatedImageSha256"]
        gate_stop = _evaluate_visual_gate(review, rules, review_bindings)
        if not candidate_unchanged:
            if isinstance(review, dict):
                review["decision"] = rules["visualReviewContract"]["decisionValues"]["rejected"]
                review["decisionEvidence"] = {"candidateBytesUnchanged": False}
            gate_stop = _stop(
                rules,
                "failed",
                "externalFailure",
                "视觉审核期间候选图字节发生变化，审核绑定已失效。",
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

        analysis = _adapter_call(rules, "analyze_approved", adapters.analyze_approved, approved_path)
        if analysis.get("visualFactSourceSha256") != _sha_file(approved_path):
            raise _stop(rules, "failed", "externalFailure", "模板分析未绑定当前确认模板图。", {})
        _atomic_write_new(output_dir / "template-analysis.json", _json_bytes(analysis))
        _record_artifact(manifest, output_dir, "template-analysis.json", p3, [approved_rel, review_name])
        _advance(manifest, rules, p3, timestamp)
        _persist_manifest(output_dir, manifest)

        editable = _compile_editable_spec(analysis, rules)
        _atomic_write_new(output_dir / "editable-template-spec.json", _json_bytes(editable))
        _record_artifact(manifest, output_dir, "editable-template-spec.json", p4, ["template-analysis.json"])
        _advance(manifest, rules, p4, timestamp)
        _persist_manifest(output_dir, manifest)

        hidden = _compile_hidden_spec(analysis, editable, rules)
        _atomic_write_new(output_dir / "hidden-template-spec.json", _json_bytes(hidden))
        _record_artifact(manifest, output_dir, "hidden-template-spec.json", p5, ["template-analysis.json", "editable-template-spec.json"])
        draft = _compile_draft(template_key, source_analysis.get("imageSize", "1024x1024"), editable, hidden)
        _atomic_write_new(output_dir / "gallery-template.draft.json", _json_bytes(draft))
        _record_artifact(manifest, output_dir, "gallery-template.draft.json", p5, ["editable-template-spec.json", "hidden-template-spec.json"])
        _advance(manifest, rules, p5, timestamp)
        _persist_manifest(output_dir, manifest)

        semantic_audit_content = _semantic_audit_payload(draft, editable)
        compiled_content_sha = _sha_bytes(_canonical_bytes(semantic_audit_content))
        semantic_audit_request = copy.deepcopy(semantic_audit_content)
        audit_request_sha = _sha_bytes(_canonical_bytes(semantic_audit_request))
        semantic_audit = _adapter_call(
            rules,
            "audit_semantics",
            adapters.audit_semantics,
            semantic_audit_request,
        )
        compiled_content_unchanged = (
            _sha_bytes(_canonical_bytes(_semantic_audit_payload(draft, editable)))
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
        validation = _validation_report(draft, editable, source_analysis, review, semantic_audit, rules)
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
                and str(receipt.get("url", "")).startswith("https://")
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
            receipt = _adapter_call(rules, "upload", adapters.upload, approved_path, object_key)
            if receipt.get("imageSha256") != _sha_file(approved_path) or not str(receipt.get("url", "")).startswith("https://"):
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
