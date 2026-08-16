from __future__ import annotations

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


class WorkflowAdapters(Protocol):
    def analyze_source(self, source_image: Path) -> dict[str, Any]: ...
    def generate(self, source_image: Path, generation_package: dict[str, Any]) -> dict[str, Any]: ...
    def inspect_generated(self, generated_image: Path) -> dict[str, Any]: ...
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
    required_artifacts: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    expected_identity = {
        "productionItemId": production_item_id,
        "templateKey": template_key,
        "sourceImageSha256": source_sha256,
    }
    for field, expected in expected_identity.items():
        if manifest.get(field) != expected:
            errors.append(f"{field} mismatch")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        return [*errors, "artifact lineage missing"]
    for required in required_artifacts:
        if required not in artifacts:
            errors.append(f"{required} missing from lineage")
    resolved_root = output_dir.resolve()
    for name, artifact in artifacts.items():
        artifact_path = (resolved_root / artifact.get("path", name)).resolve()
        if not artifact_path.is_relative_to(resolved_root):
            errors.append(f"{name} escapes production item")
            continue
        if not artifact_path.is_file():
            errors.append(f"{name} missing")
            continue
        if _sha_file(artifact_path) != artifact.get("sha256"):
            errors.append(f"{name} digest mismatch")
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
        "revision": 1,
        "dependsOn": dependencies,
    }


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


def _plan_replacement(source_analysis: dict[str, Any], rules: dict[str, Any], template_key: str) -> dict[str, Any]:
    category = source_analysis["target"]["category"]
    if category == "unknown" or category not in rules["sourceCategories"]:
        raise _stop(
            rules,
            "needs_input",
            "unknownCategory",
            "来源主体类别无法支持自主替换，需要补充识别。",
            {"category": category},
        )
    candidates = [
        candidate
        for candidate in source_analysis.get("replacementPool", [])
        if candidate.get("category") == category
        and candidate.get("semanticCompatible") is True
        and candidate.get("visualCompatible") is True
        and candidate.get("rightsAndSafety") == "pass"
    ]
    if not candidates:
        raise _stop(
            rules,
            "blocked",
            "noCompatibleReplacement",
            "没有通过同类、视觉与权利硬过滤的自主替换值。",
            {"category": category},
        )
    selected = sorted(candidates, key=lambda item: (-float(item["score"]), item["value"]))[0]
    closure = source_analysis.get("dependencyClosure", [])
    if not closure:
        raise _stop(
            rules,
            "blocked",
            "noCompatibleReplacement",
            "主要替换目标缺少依赖闭包。",
            {"category": category},
        )
    return {
        "artifactType": "replacement-plan",
        "schemaVersion": rules["schemaVersion"],
        "templateKey": template_key,
        "strategy": {"source": "autonomous", "decisionSource": "autonomous"},
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
                "decisionSource": "autonomous",
            }
        ],
        "dependencyClosure": closure,
        "changedSet": [
            {"kind": "primary", "value": source_analysis["target"]["role"], "decisionSource": "autonomous"},
            *[
                {"kind": "dependency", "value": item["value"], "dependencyType": item["type"], "decisionSource": "autonomous"}
                for item in closure
            ],
        ],
        "frozenSet": source_analysis["frozenSet"],
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


def _assert_visual_gate(review: dict[str, Any], rules: dict[str, Any]) -> None:
    failures = [name for name in rules["visualHardGates"] if review.get("hardGates", {}).get(name) is not True]
    failures.extend(
        name for name in rules["visualDimensions"] if review.get("visualDimensions", {}).get(name, {}).get("pass") is not True
    )
    if failures:
        raise _stop(
            rules,
            "blocked",
            "visualHardFailure",
            "生成图未通过最小视觉硬门禁，必须修正或重生成。",
            {"failedGates": failures},
        )


def _compile_editable_spec(analysis: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    slots = [slot for slot in analysis["slotCandidates"] if all(slot["valueGates"].values())]
    budget = rules["slotBudget"]
    if not budget["minimum"] <= len(slots) <= budget["maximum"]:
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "高价值槽位数量不在常态预算内。",
            {"slotCount": len(slots)},
        )
    if analysis.get("hasPrimarySubject") and not any(slot["semanticRole"] == "subject" for slot in slots):
        raise _stop(
            rules,
            "blocked",
            "contractFailure",
            "画面存在明显主体，但高价值槽位没有主体入口。",
            {},
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
    return {
        "artifactType": "editable-template-spec",
        "schemaVersion": rules["schemaVersion"],
        "visualFactSourceSha256": analysis["visualFactSourceSha256"],
        "title": analysis["neutralTitle"],
        "description": analysis["neutralDescription"],
        "slots": slots,
        "slotSuggestionPools": {slot["id"]: slot["suggestions"] for slot in slots},
        "promptTemplate": analysis["promptTemplate"],
        "freeEditableContent": analysis["freeEditableContent"],
        "tags": analysis["tags"],
    }


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
    instruction = analysis["promptEnhancement"]["instruction"]
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
            "lockedConstraints": analysis["promptEnhancement"]["lockedConstraints"],
            "preserve": analysis["promptEnhancement"]["preserve"],
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


PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_-]*)\b[^}]*\}\}")
SENTENCE_PUNCTUATION = re.compile(r"[，。！？；,.!?;]")


def _resolve_prompt(prompt_template: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), prompt_template)


def _semantic_audit_payload(draft: dict[str, Any], editable: dict[str, Any]) -> dict[str, Any]:
    return {
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
    }


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
    semantic_audit_contract_valid = (
        semantic_audit.get("artifactType") == "semantic-audit"
        and semantic_audit.get("schemaVersion") == rules["schemaVersion"]
    )
    semantic_audit_bound = (
        semantic_audit_contract_valid
        and semantic_audit.get("contentSha256") == audited_content_sha
        and semantic_audit.get("observedContentSha256") == audited_content_sha
    )
    semantic_audit_checks = {
        name: semantic_audit.get("checks", {}).get(name) is True for name in rules["semanticAuditChecks"]
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
    approved_names = sorted(
        name for name in manifest["artifacts"] if name.startswith("evidence/approved-template-image.")
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

    The request accepts one source image and no shared state. In this Issue #2 slice,
    replacement strategy is intentionally autonomous; explicit and batch strategies
    are reserved for later tickets.
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
    if invalid_identifiers:
        return ProductionResult(
            "needs_input",
            str(production_item_id or "invalid-production-item"),
            rules["resultStates"]["needs_input"],
            output_root_path,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message=f"生产请求包含非法标识符：{', '.join(invalid_identifiers)}",
        )
    source_image = Path(request["sourceImage"]).resolve()
    if not source_image.is_file():
        raise FileNotFoundError(source_image)
    source_sha = _sha_file(source_image)
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
    if manifest_path.exists():
        existing = _load_json(manifest_path)
        completed_artifacts = ("production-pin.json", "gallery-template.json", "final-validation-report.json")
        identity_errors = _production_item_integrity_errors(
            output_dir,
            existing,
            production_item_id=item_id,
            template_key=template_key,
            source_sha256=source_sha,
            required_artifacts=completed_artifacts if existing.get("state") == rules["resultStates"]["completed"] else (),
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
                required_artifacts=(
                    "production-pin.json",
                    "gallery-template.draft.json",
                    "validation-report.json",
                    "asset-receipt.json",
                ),
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
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "artifactType": "production-manifest",
        "schemaVersion": rules["schemaVersion"],
        "productionItemId": item_id,
        "templateKey": template_key,
        "revision": 1,
        "sourceImageSha256": source_sha,
        "phase": None,
        "state": rules["initialState"],
        "outcome": None,
        "history": [],
        "artifacts": {},
        "historicalExperienceEvidence": rules["historicalExperienceEvidence"],
    }
    try:
        pin = _build_pin(rules, release)
        _atomic_write_new(output_dir / "production-pin.json", _json_bytes(pin))
        _record_artifact(manifest, output_dir, "production-pin.json", p0, [])
        evidence_source = output_dir / "evidence" / f"source-image{source_image.suffix.lower()}"
        _atomic_write_new(evidence_source, source_image.read_bytes())
        _record_artifact(manifest, output_dir, str(evidence_source.relative_to(output_dir)), p0, [])
        source_analysis = _adapter_call(rules, "analyze_source", adapters.analyze_source, source_image)
        if source_analysis.get("sourceImageSha256") != source_sha:
            raise _stop(rules, "failed", "externalFailure", "来源分析证据与输入图片 SHA 不一致。", {})
        _atomic_write_new(output_dir / "source-analysis.json", _json_bytes(source_analysis))
        _record_artifact(manifest, output_dir, "source-analysis.json", p0, [str(evidence_source.relative_to(output_dir))])
        _advance(manifest, rules, p0, timestamp)
        _persist_manifest(output_dir, manifest)

        plan = _plan_replacement(source_analysis, rules, template_key)
        _atomic_write_new(output_dir / "replacement-plan.json", _json_bytes(plan))
        _record_artifact(manifest, output_dir, "replacement-plan.json", p1, ["source-analysis.json"])
        _advance(manifest, rules, p1, timestamp)
        _persist_manifest(output_dir, manifest)

        generation_package = _compile_generation_package(plan, source_analysis)
        _atomic_write_new(output_dir / "generation-package.json", _json_bytes(generation_package))
        _record_artifact(manifest, output_dir, "generation-package.json", p2, ["replacement-plan.json"])
        generated = _adapter_call(rules, "generate", adapters.generate, source_image, generation_package)
        generated_extension = str(generated.get("extension", ""))
        if re.fullmatch(rules["identifiers"]["imageExtensionPattern"], generated_extension) is None:
            raise _stop(
                rules,
                "failed",
                "externalFailure",
                "生成适配器返回了不安全的图片扩展名。",
                {"extension": generated_extension},
            )
        candidate_rel = f"evidence/generated-candidate-image{generated_extension}"
        candidate_path = output_dir / candidate_rel
        _atomic_write_new(candidate_path, generated["imageBytes"])
        _record_artifact(manifest, output_dir, candidate_rel, p2, ["generation-package.json"])
        review = _adapter_call(rules, "inspect_generated", adapters.inspect_generated, candidate_path)
        if review.get("generatedImageSha256") != _sha_file(candidate_path):
            raise _stop(rules, "failed", "externalFailure", "视觉审核证据与生成图 SHA 不一致。", {})
        _atomic_write_new(output_dir / "visual-review.json", _json_bytes(review))
        _record_artifact(manifest, output_dir, "visual-review.json", p2, [candidate_rel, "generation-package.json"])
        _assert_visual_gate(review, rules)
        approved_rel = f"evidence/approved-template-image{generated_extension}"
        approved_path = output_dir / approved_rel
        _atomic_write_new(approved_path, candidate_path.read_bytes())
        _record_artifact(manifest, output_dir, approved_rel, p2, [candidate_rel, "visual-review.json"])
        _advance(manifest, rules, p2, timestamp)
        _persist_manifest(output_dir, manifest)

        analysis = _adapter_call(rules, "analyze_approved", adapters.analyze_approved, approved_path)
        if analysis.get("visualFactSourceSha256") != _sha_file(approved_path):
            raise _stop(rules, "failed", "externalFailure", "模板分析未绑定当前确认模板图。", {})
        _atomic_write_new(output_dir / "template-analysis.json", _json_bytes(analysis))
        _record_artifact(manifest, output_dir, "template-analysis.json", p3, [approved_rel, "visual-review.json"])
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
        semantic_audit = _adapter_call(
            rules,
            "audit_semantics",
            adapters.audit_semantics,
            semantic_audit_content,
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
            ["gallery-template.draft.json", "visual-review.json", "semantic-audit.json"],
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
        )
    except WorkflowStop as stop:
        manifest["state"] = stop.state
        manifest["outcome"] = stop.outcome
        manifest["error"] = {"code": stop.error_code, "message": stop.message, "evidence": stop.evidence}
        _persist_manifest(output_dir, manifest)
        return ProductionResult(stop.outcome, item_id, stop.state, output_dir, error_code=stop.error_code, message=stop.message)
