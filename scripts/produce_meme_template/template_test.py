from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image, UnidentifiedImageError

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    compact_json_line_bytes as _json_bytes,
    load_json as _load_json,
    sha256_bytes as _sha_bytes,
)
from .release_management import runtime_production_pin
from .validation import is_valid_https_url
from .workflow import formal_template_contract_valid, image_bytes_match_output_format
from .workflow_core import GALLERY_SCHEMA_PATH


REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "contracts" / "machine-rules.json"
PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


class RetryableTemplateTestGeneration(RuntimeError):
    pass


class TemplateTestNeedsInput(RuntimeError):
    pass


class TemplateTestIntegrityError(ValueError):
    pass


class TemplateTestPermanentGenerationFailure(RuntimeError):
    pass


class TemplateTestRoutedGenerationStop(RuntimeError):
    def __init__(self, failure_class: str) -> None:
        super().__init__(failure_class)
        self.failure_class = failure_class


class TemplateTestReviewEvidenceInvalid(RuntimeError):
    pass


@dataclass(frozen=True)
class TemplateTestResult:
    outcome: str
    invocation_id: str
    state: str
    output_dir: Path
    report_path: Path | None = None
    error_code: str | None = None
    message: str | None = None
    resumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        fields = _rules()["templateTestContract"]["resultFields"]
        return {
            fields["outcome"]: self.outcome,
            fields["invocationIdentity"]: self.invocation_id,
            fields["state"]: self.state,
            fields["outputDirectory"]: str(self.output_dir),
            fields["reportPath"]: (
                None if self.report_path is None else str(self.report_path)
            ),
            fields["errorCode"]: self.error_code,
            fields["message"]: self.message,
            fields["resumed"]: self.resumed,
        }


def _rules() -> dict[str, Any]:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def _write_mutable(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(_json_bytes(value))
        handle.flush()
    temporary.replace(path)


def _write_new_or_same(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError:
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise ValueError(f"immutable artifact conflict: {path.name}")


def _call_with_image_snapshot(
    image_path: Path, operation: Callable[..., Any], *payloads: Any
) -> Any:
    """Keep adapter code outside the immutable T1 artifact namespace."""
    original = image_path.read_bytes()
    copied_payloads = copy.deepcopy(payloads)
    with tempfile.TemporaryDirectory() as temporary:
        snapshot = Path(temporary) / image_path.name
        snapshot.write_bytes(original)
        result = operation(snapshot, *copied_payloads)
        if not snapshot.is_file() or snapshot.read_bytes() != original:
            raise RuntimeError("adapter changed its image snapshot")
    if not image_path.is_file() or image_path.read_bytes() != original:
        raise TemplateTestIntegrityError("core image artifact changed during adapter call")
    if copied_payloads != payloads:
        raise RuntimeError("adapter changed its frozen request payload")
    return result


def _safe_output_dir(root: Path, invocation_id: str) -> Path | None:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    lexical = root / invocation_id
    if lexical.is_symlink():
        return None
    if lexical.exists() and lexical.resolve() != lexical:
        return None
    return lexical


def _formal_template_errors(template: Any) -> list[str]:
    if not isinstance(template, dict):
        return ["formal template must be an object"]
    rules = _rules()
    top_level = rules["formalProjection"]["topLevel"]
    input_contract = rules["slotCompilationContract"]["inputContract"]
    input_fields = input_contract["fields"]
    slot_fields = input_contract["slotFields"]
    schema = json.loads(GALLERY_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [error.message for error in validator.iter_errors(template)]
    cover = template.get(top_level["coverAsset"])
    reference = template.get(top_level["referenceAsset"])
    if not (
        isinstance(cover, str)
        and cover == reference
        and is_valid_https_url(cover)
    ):
        errors.append("cover and referenceImage must be the same HTTPS URL")
    if not formal_template_contract_valid(template, rules):
        errors.append("formal projection contract invalid")
    input_schema = template.get(top_level["userInputSchema"])
    input_slots = (
        input_schema.get(input_fields["slots"])
        if isinstance(input_schema, dict)
        and input_schema.get(input_fields["version"]) == input_contract["version"]
        else None
    )
    prompt_template = template.get(top_level["userPromptTemplate"])
    if isinstance(input_slots, list) and isinstance(prompt_template, str):
        input_ids = [
            item.get(slot_fields["identity"])
            for item in input_slots
            if isinstance(item, dict)
            and isinstance(item.get(slot_fields["identity"]), str)
        ]
        referenced_heads: set[str] = set()
        for match in PLACEHOLDER.finditer(prompt_template):
            for term in _placeholder_terms(match.group(1)):
                if len(term) >= 2 and term[0] == term[-1] == '"':
                    continue
                head = term.split(".", 1)[0].strip()
                if head:
                    referenced_heads.add(head)
        if len(input_ids) != len(input_slots) or len(input_ids) != len(
            set(input_ids)
        ):
            errors.append("formal input ids must be complete and unique")
        elif referenced_heads != set(input_ids):
            errors.append("prompt placeholders and formal input ids must match")
    return errors


def _placeholder_terms(expression: str) -> list[str]:
    terms: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, character in enumerate(expression):
        if escaped:
            escaped = False
        elif character == "\\" and quoted:
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif character == "|" and not quoted:
            terms.append(expression[start:index].strip())
            start = index + 1
    terms.append(expression[start:].strip())
    return terms


def _resolve_prompt(
    prompt_template: str,
    slot_values: dict[str, str],
    input_schema: list[dict[str, Any]],
) -> str:
    inputs = {item["id"]: item for item in input_schema}

    def replacement(match: re.Match[str]) -> str:
        terms = _placeholder_terms(match.group(1))
        for term in terms:
            if len(term) >= 2 and term[0] == term[-1] == '"':
                try:
                    literal = json.loads(term)
                except json.JSONDecodeError:
                    continue
                if isinstance(literal, str) and literal:
                    return literal
            path = [part.strip() for part in term.split(".")]
            head = path[0]
            value = slot_values.get(head)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise ValueError(f"placeholder has no resolved value: {match.group(0)}")

    resolved = PLACEHOLDER.sub(replacement, prompt_template).strip()
    if not resolved or PLACEHOLDER.search(resolved):
        raise ValueError("resolved prompt is incomplete")
    return resolved


def _case_prompt(
    template: dict[str, Any],
    case: dict[str, Any],
    contract: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    fields = contract["caseFields"]
    mode = case[fields["mode"]]
    if mode == contract["modes"]["freeEdit"]:
        prompt = case.get(fields["freePrompt"])
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("free edit requires a prompt")
        return prompt.strip(), {fields["freePrompt"]: prompt.strip()}
    slot_values = case.get(fields["slotValues"])
    if not isinstance(slot_values, dict):
        raise ValueError("slot edit requires slot values")
    top_level = rules["formalProjection"]["topLevel"]
    input_contract = rules["slotCompilationContract"]["inputContract"]
    input_fields = input_contract["fields"]
    slot_fields = input_contract["slotFields"]
    input_slots = template[top_level["userInputSchema"]][input_fields["slots"]]
    input_ids = {
        item.get(slot_fields["identity"])
        for item in input_slots
        if isinstance(item, dict)
        and isinstance(item.get(slot_fields["identity"]), str)
    }
    if not (
        set(slot_values) <= input_ids
        and all(
            isinstance(key, str)
            and isinstance(value, str)
            and value.strip()
            for key, value in slot_values.items()
        )
    ):
        raise ValueError("slot values do not match the formal input schema")
    input_by_id = {
        item[slot_fields["identity"]]: item
        for item in input_slots
        if isinstance(item, dict)
    }
    if any(
        slot_fields["text"] not in input_by_id[slot_id]
        for slot_id in slot_values
    ):
        raise ValueError(
            "image-only slots require a binary test asset and are not string slot values"
        )
    text_capable_ids = {
        slot_id
        for slot_id, item in input_by_id.items()
        if slot_fields["text"] in item
    }
    if not slot_values and text_capable_ids:
        raise ValueError("slot edit requires at least one text-capable slot value")
    normalized = {key: value.strip() for key, value in slot_values.items()}
    return _resolve_prompt(
        template[top_level["userPromptTemplate"]], normalized, input_slots
    ), {
        fields["slotValues"]: normalized
    }


def _compile_actual_prompt(
    resolved_prompt: str,
    runtime_semantics: dict[str, Any],
    rules: dict[str, Any],
) -> str:
    """Compile the author prompt and structured runtime contract for a real model call."""
    runtime_contract = rules["runtimeSemanticsContract"]
    runtime_fields = runtime_contract["fields"]
    target_fields = runtime_contract["targetInstanceFields"]
    binding_fields = runtime_contract["inputBindingFields"]
    visual_fields = runtime_contract["visualContractFields"]
    targets = runtime_semantics[runtime_fields["targetInstances"]]
    bindings = runtime_semantics[runtime_fields["inputBindings"]]
    visual = runtime_semantics[runtime_fields["visualContract"]]
    target_by_id = {
        target[target_fields["identity"]]: target for target in targets
    }

    target_text = "；".join(
        f"{target[target_fields['role']]}位于{target[target_fields['region']]}"
        for target in targets
    )
    binding_texts: list[str] = []
    for input_id, binding in bindings.items():
        target_roles = "、".join(
            target_by_id[target_id][target_fields["role"]]
            for target_id in binding[binding_fields["targetIdentities"]]
        )
        operation = binding[binding_fields["operation"]]
        if operation == runtime_contract["operations"]["replaceIdentity"]:
            policy = binding[
                binding_fields["identityBindingPolicy"]
            ]
            policy_text = (
                "一对一接管"
                if policy == runtime_contract["identityBindingPolicies"]["oneToOne"]
                else "以同一来源身份同步接管全部重复实例"
            )
            binding_texts.append(
                f"输入 {input_id} {policy_text}{target_roles}，"
                "只提供身份线索并按模板媒介完整重绘"
            )
        else:
            binding_texts.append(
                f"输入 {input_id} 完整接管{target_roles}，"
                "先清除各目标的旧内容与残留，再由当前输入完整替换，"
                f"分布策略为 {binding[binding_fields['distributionPolicy']]}"
            )

    sections = [
        resolved_prompt,
        f"媒介：{visual[visual_fields['medium']]}。",
        f"画风特征：{'；'.join(visual[visual_fields['styleTraits']])}。",
        f"构图：{'；'.join(visual[visual_fields['composition']])}。",
        f"关系：{'；'.join(visual[visual_fields['relations']])}。",
    ]
    if visual[visual_fields["colorAndLight"]]:
        sections.append(
            f"色彩与光线：{'；'.join(visual[visual_fields['colorAndLight']])}。"
        )
    sections.extend(
        [
            f"目标实例：{target_text}。",
            f"输入接管：{'；'.join(binding_texts)}。",
        ]
    )
    return "\n".join(sections)


def _normalized_request(
    request: Any, template_path: Path, contract: dict[str, Any]
) -> dict[str, Any]:
    fields = contract["requestFields"]
    case_fields = contract["caseFields"]
    if not isinstance(request, dict) or set(request) != set(fields.values()):
        raise ValueError("template test request shape invalid")
    revision = request.get(fields["templateRevision"])
    invocation_id = request.get(fields["invocationIdentity"])
    cases = request.get(fields["cases"])
    identity_pattern = re.compile(contract["identityPattern"])
    if not (
        isinstance(revision, int)
        and not isinstance(revision, bool)
        and revision > 0
        and isinstance(invocation_id, str)
        and identity_pattern.fullmatch(invocation_id)
        and isinstance(cases, list)
        and 1 <= len(cases) <= contract["maximumCases"]
    ):
        raise ValueError("template test request values invalid")
    expected_case_keys = set(case_fields.values())
    seen: set[str] = set()
    normalized_cases: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict) or not set(case) <= expected_case_keys:
            raise ValueError("template test case shape invalid")
        case_id = case.get(case_fields["caseIdentity"])
        mode = case.get(case_fields["mode"])
        if not (
            isinstance(case_id, str)
            and identity_pattern.fullmatch(case_id)
            and case_id not in seen
            and mode in contract["modes"].values()
        ):
            raise ValueError("template test case identity or mode invalid")
        seen.add(case_id)
        required = {
            case_fields["caseIdentity"],
            case_fields["mode"],
            (
                case_fields["slotValues"]
                if mode == contract["modes"]["slotEdit"]
                else case_fields["freePrompt"]
            ),
        }
        if set(case) != required:
            raise ValueError("template test case fields do not match mode")
        normalized_cases.append(case)
    return {
        fields["templateJsonPath"]: str(template_path),
        fields["templateRevision"]: revision,
        fields["invocationIdentity"]: invocation_id,
        fields["cases"]: normalized_cases,
    }


def _reference_extension(payload: bytes) -> str:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.verify()
            image_format = image.format
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError("template reference image is not decodable") from exc
    extensions = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
    if image_format not in extensions:
        raise ValueError("template reference image format unsupported")
    return extensions[image_format]


def _fetch_reference(
    adapters: Any, url: str, output_dir: Path, contract: dict[str, Any]
) -> tuple[Path, str]:
    stem = contract["artifactNames"]["referenceImageStem"]
    existing = list(output_dir.glob(f"{stem}.*"))
    if len(existing) == 1 and existing[0].is_file() and not existing[0].is_symlink():
        payload = existing[0].read_bytes()
        _reference_extension(payload)
        return existing[0], _sha_bytes(payload)
    if existing:
        raise ValueError("template reference artifact set invalid")
    fetch = getattr(adapters, "fetch_template_image", None)
    if not callable(fetch):
        raise RuntimeError("generation adapter cannot fetch the template image")
    payload = fetch(url)
    extension = _reference_extension(payload)
    path = output_dir / f"{stem}{extension}"
    _write_new_or_same(path, payload)
    return path, _sha_bytes(payload)


def _frozen_test_inputs_match(
    *,
    template_path: Path,
    template_bytes: bytes,
    pin_path: Path,
    pin: dict[str, Any],
    request_path: Path,
    normalized_request: dict[str, Any],
    reference_path: Path,
    reference_sha: str,
) -> bool:
    expected = (
        (template_path, template_bytes),
        (pin_path, _json_bytes(pin)),
        (request_path, _json_bytes(normalized_request)),
    )
    return bool(
        all(
            path.is_file()
            and not path.is_symlink()
            and path.read_bytes() == payload
            for path, payload in expected
        )
        and reference_path.is_file()
        and not reference_path.is_symlink()
        and _sha_bytes(reference_path.read_bytes()) == reference_sha
    )


def _prepared_wal(
    task: dict[str, Any], rules: dict[str, Any], timestamp: str
) -> dict[str, Any]:
    contract = rules["generationExecutionContract"]
    fields = contract["walFields"]
    task_fields = contract["taskFields"]
    return {
        fields["artifactType"]: contract["artifactTypes"]["wal"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["taskIdentity"]: task[task_fields["taskIdentity"]],
        fields["taskSha256"]: _sha_bytes(_json_bytes(task)),
        fields["previousWalSha256"]: None,
        fields["revision"]: 1,
        fields["status"]: contract["walStatuses"]["prepared"],
        fields["provider"]: None,
        fields["model"]: None,
        fields["providerRequestIdentity"]: None,
        fields["providerOutputIdentity"]: None,
        fields["outputSha256"]: None,
        fields["outputAssets"]: [],
        fields["pollAttemptCount"]: 0,
        fields["failureClass"]: None,
        fields["failureReason"]: None,
        fields["updatedAt"]: timestamp,
    }


def _execution_identity_valid(value: Any, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _safe_failure_reason(value: Any, execution: dict[str, Any]) -> str:
    raw = value if isinstance(value, str) else type(value).__name__
    contract = execution["persistedErrorSanitization"]
    return contract["digestPrefix"] + hashlib.sha256(raw.encode()).hexdigest()[
        : contract["digestLength"]
    ]


def _generation_failure_route(
    failure_class: str, rules: dict[str, Any]
) -> dict[str, Any]:
    execution = rules["generationExecutionContract"]
    contract = rules["templateTestContract"]
    role = next(
        role
        for role, value in execution["failureClasses"].items()
        if value == failure_class
    )
    route = contract["generationFailureRoutes"][role]
    execution_route = execution["failureRoutes"][role]
    return {
        "outcome": execution_route["outcomeRole"],
        "state": contract["states"][route["stateRole"]],
        "errorCode": contract["errorCodes"][route["errorCodeRole"]],
    }


def _wal_semantics_valid(
    wal: Any, task: dict[str, Any], execution: dict[str, Any]
) -> bool:
    fields = execution["walFields"]
    task_fields = execution["taskFields"]
    if not (
        isinstance(wal, dict)
        and set(wal) == set(fields.values())
        and wal.get(fields["taskIdentity"])
        == task[task_fields["taskIdentity"]]
        and wal.get(fields["taskSha256"]) == _sha_bytes(_json_bytes(task))
        and wal.get(fields["revision"]) == task[task_fields["revision"]]
        and wal.get(fields["status"]) in execution["walStatuses"].values()
        and isinstance(wal.get(fields["pollAttemptCount"]), int)
        and not isinstance(wal.get(fields["pollAttemptCount"]), bool)
        and isinstance(wal.get(fields["updatedAt"]), str)
        and wal[fields["updatedAt"]].strip()
    ):
        return False
    status = wal[fields["status"]]
    attempts = wal[fields["pollAttemptCount"]]
    previous = wal[fields["previousWalSha256"]]
    provider = wal[fields["provider"]]
    model = wal[fields["model"]]
    request_id = wal[fields["providerRequestIdentity"]]
    output_id = wal[fields["providerOutputIdentity"]]
    output_sha = wal[fields["outputSha256"]]
    output_assets = wal[fields["outputAssets"]]
    failure_class = wal[fields["failureClass"]]
    failure_reason = wal[fields["failureReason"]]
    budget = execution["retryBudgets"]["retryable"]
    empty_output = output_id is None and output_sha is None and output_assets == []
    no_failure = failure_class is None and failure_reason is None
    valid_provider = bool(
        _execution_identity_valid(provider, execution["providerIdentityPattern"])
        and _execution_identity_valid(model, execution["modelIdentityPattern"])
    )
    valid_request = _execution_identity_valid(
        request_id, execution["opaqueExecutionIdentityPattern"]
    )
    valid_previous = isinstance(previous, str) and re.fullmatch(
        r"[0-9a-f]{64}", previous
    ) is not None
    if status == execution["walStatuses"]["prepared"]:
        return bool(
            previous is None
            and provider is None
            and model is None
            and request_id is None
            and empty_output
            and no_failure
            and attempts == 0
        )
    if status == execution["walStatuses"]["submitted"]:
        return bool(
            valid_previous
            and valid_provider
            and valid_request
            and empty_output
            and no_failure
            and 0 <= attempts <= budget
        )
    if status == execution["walStatuses"]["succeeded"]:
        asset_fields = execution["outputAssetFields"]
        intent = task[task_fields["requestIntent"]]
        intent_fields = execution["requestIntentFields"]
        image_count = intent[intent_fields["imageCount"]]
        primary_index = intent[intent_fields["primaryOutputIndex"]]
        assets_valid = bool(
            isinstance(output_assets, list)
            and len(output_assets) == image_count
            and all(
                isinstance(item, dict)
                and set(item) == set(asset_fields.values())
                and _execution_identity_valid(
                    item.get(asset_fields["providerOutputIdentity"]),
                    execution["opaqueExecutionIdentityPattern"],
                )
                and isinstance(item.get(asset_fields["sha256"]), str)
                and re.fullmatch(r"[0-9a-f]{64}", item[asset_fields["sha256"]])
                for item in output_assets
            )
            and len(
                {
                    item[asset_fields["providerOutputIdentity"]]
                    for item in output_assets
                }
            )
            == len(output_assets)
        )
        return bool(
            valid_previous
            and valid_provider
            and valid_request
            and no_failure
            and 1 <= attempts <= budget
            and _execution_identity_valid(
                output_id, execution["opaqueExecutionIdentityPattern"]
            )
            and isinstance(output_sha, str)
            and re.fullmatch(r"[0-9a-f]{64}", output_sha)
            and assets_valid
            and output_assets[primary_index][
                asset_fields["providerOutputIdentity"]
            ]
            == output_id
            and output_assets[primary_index][asset_fields["sha256"]]
            == output_sha
        )
    if status == execution["walStatuses"]["failed"]:
        if not (
            valid_previous
            and valid_provider
            and empty_output
            and failure_class in execution["failureClasses"].values()
            and isinstance(failure_reason, str)
            and failure_reason.strip()
            and 0 <= attempts <= budget
        ):
            return False
        if failure_class == execution["failureClasses"]["submissionUnknown"]:
            return request_id is None and attempts == 0
        if failure_class == execution["failureClasses"]["retryable"]:
            return bool(valid_request and 1 <= attempts < budget)
        return request_id is None or valid_request
    return False


def _review_evidence_valid(
    review: Any,
    review_request: dict[str, Any],
    contract: dict[str, Any],
) -> bool:
    fields = contract["reviewFields"]
    return bool(
        isinstance(review, dict)
        and set(review) == set(fields.values())
        and all(
            review.get(fields[role]) == review_request[fields[role]]
            for role in (
                "templateJsonSha256",
                "testCaseSha256",
                "generatedImageSha256",
            )
        )
        and isinstance(review.get(fields["pass"]), bool)
        and isinstance(review.get(fields["visibleDeviations"]), list)
        and all(
            isinstance(item, str) and item.strip()
            for item in review[fields["visibleDeviations"]]
        )
        and isinstance(review.get(fields["explanation"]), str)
    )


def _completed_report_valid(
    report: Any,
    *,
    output_dir: Path,
    contract: dict[str, Any],
    invocation_id: str,
    template: dict[str, Any],
    template_sha: str,
    template_revision: int,
    pin: dict[str, Any],
    request_sha: str,
    case_ids: list[str],
    template_path: Path,
    rules: dict[str, Any],
    normalized_cases: list[dict[str, Any]],
    prompts: list[tuple[str, dict[str, Any]]],
) -> bool:
    fields = contract["reportFields"]
    case_fields = contract["caseReportFields"]
    source_case_fields = contract["caseFields"]
    release_contract = rules["releaseManagementContract"]
    pin_fields = release_contract["productionPinFields"]
    skill_fields = release_contract["productionPinSkillFields"]
    expected_tester_version = pin[pin_fields["skill"]][skill_fields["version"]]
    outcome = report.get(fields["outcome"]) if isinstance(report, dict) else None
    cases = report.get(fields["cases"]) if isinstance(report, dict) else None
    if not (
        isinstance(report, dict)
        and set(report) == set(fields.values())
        and report.get(fields["artifactType"]) == contract["artifactTypes"]["report"]
        and report.get(fields["schemaVersion"]) == rules["schemaVersion"]
        and report.get(fields["invocationIdentity"]) == invocation_id
        and report.get(fields["templateKey"]) == template["key"]
        and report.get(fields["templateRevision"]) == template_revision
        and report.get(fields["templateJsonPath"]) == str(template_path)
        and report.get(fields["templateJsonSha256"]) == template_sha
        and report.get(fields["templateImageUrl"]) == template["referenceImage"]
        and report.get(fields["testerVersion"]) == expected_tester_version
        and report.get(fields["productionPin"]) == pin
        and report.get(fields["requestSha256"]) == request_sha
        and outcome in {"completed", "failed"}
        and isinstance(report.get(fields["startedAt"]), str)
        and report[fields["startedAt"]].strip()
        and isinstance(report.get(fields["completedAt"]), str)
        and report[fields["completedAt"]].strip()
        and (
            (outcome == "completed" and report.get(fields["errorCode"]) is None)
            or (
                outcome == "failed"
                and report.get(fields["errorCode"])
                in {
                    contract["errorCodes"]["externalFailure"],
                    contract["errorCodes"]["generationPermanent"],
                }
            )
        )
        and isinstance(cases, list)
        and cases
        and [
            item.get(case_fields["caseIdentity"])
            for item in cases
            if isinstance(item, dict)
        ]
        == (
            case_ids
            if outcome == "completed"
            else case_ids[: len(cases)]
        )
    ):
        return False
    resolved_root = output_dir.resolve()
    for index, item in enumerate(cases):
        if not isinstance(item, dict) or set(item) != set(case_fields.values()):
            return False
        item_outcome = item.get(case_fields["outcome"])
        expected_item_outcome = (
            "completed"
            if outcome == "completed" or index < len(cases) - 1
            else "failed"
        )
        if item_outcome != expected_item_outcome:
            return False
        expected_case = normalized_cases[index]
        expected_prompt, expected_user_input = prompts[index]
        expected_generation_request = _case_generation_request(
            expected_case, expected_prompt, template, template_sha, rules
        )
        if not (
            item.get(case_fields["mode"])
            == expected_case[source_case_fields["mode"]]
            and item.get(case_fields["userInput"]) == expected_user_input
            and item.get(case_fields["resolvedPrompt"]) == expected_prompt
            and isinstance(item.get(case_fields["generationRequest"]), dict)
            and isinstance(item.get(case_fields["visibleDeviations"]), list)
            and all(
                isinstance(value, str) and value.strip()
                for value in item[case_fields["visibleDeviations"]]
            )
            and isinstance(item.get(case_fields["reviewPass"]), bool)
        ):
            return False
        if expected_item_outcome == "failed":
            if not (
                item.get(case_fields["errorCode"])
                == report.get(fields["errorCode"])
                and isinstance(item.get(case_fields["message"]), str)
                and item[case_fields["message"]].strip()
                and item.get(case_fields["generationRequest"])
                == expected_generation_request
                and item.get(case_fields["outputImagePath"]) is None
                and item.get(case_fields["outputImageSha256"]) is None
                and item.get(case_fields["visibleDeviations"]) == []
                and item.get(case_fields["reviewPass"]) is False
            ):
                return False
        elif (
            item.get(case_fields["errorCode"]) is not None
            or item.get(case_fields["message"]) is not None
        ):
            return False
        output_path_value = item.get(case_fields["outputImagePath"])
        output_sha = item.get(case_fields["outputImageSha256"])
        if output_path_value is None and output_sha is None:
            if item.get(case_fields["outcome"]) != "failed":
                return False
            continue
        if not isinstance(output_path_value, str) or not isinstance(output_sha, str):
            return False
        output_path = Path(output_path_value)
        if not output_path.is_absolute():
            output_path = output_dir / output_path
        try:
            resolved = output_path.resolve()
        except OSError:
            return False
        if (
            not resolved.is_relative_to(resolved_root)
            or not resolved.is_file()
            or output_path.is_symlink()
            or _sha_bytes(resolved.read_bytes()) != output_sha
        ):
            return False
    return True


def _case_generation_request(
    case: dict[str, Any],
    resolved_prompt: str,
    template: dict[str, Any],
    template_sha: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    test_contract = rules["templateTestContract"]
    execution = rules["generationExecutionContract"]
    request_fields = test_contract["generationRequestFields"]
    case_sha = _sha_bytes(_canonical_bytes(case))
    request_id = "t1-" + _sha_bytes(
        _canonical_bytes({"template": template_sha, "case": case_sha})
    )[:32]
    output_format = execution["outputFormats"][
        test_contract["defaultOutputFormatRole"]
    ]
    return {
        request_fields["requestIdentity"]: request_id,
        request_fields["prompt"]: resolved_prompt,
        request_fields["runtimeSemantics"]: copy.deepcopy(
            template["runtimeSemantics"]
        ),
        request_fields["imageCount"]: test_contract["defaultImageCount"],
        request_fields["primaryOutputIndex"]: test_contract[
            "defaultPrimaryOutputIndex"
        ],
        request_fields["imageSize"]: template.get("imageSize", "1024x1024"),
        request_fields["outputFormat"]: output_format,
    }


def _case_execution_facts(
    *,
    case: dict[str, Any],
    resolved_prompt: str,
    template: dict[str, Any],
    template_sha: str,
    pin_sha: str,
    reference_sha: str,
    rules: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    test_contract = rules["templateTestContract"]
    execution = rules["generationExecutionContract"]
    request_fields = test_contract["generationRequestFields"]
    task_fields = execution["taskFields"]
    intent_fields = execution["requestIntentFields"]
    generation_request = _case_generation_request(
        case, resolved_prompt, template, template_sha, rules
    )
    request_id = generation_request[request_fields["requestIdentity"]]
    case_sha = _sha_bytes(_canonical_bytes(case))
    output_format = generation_request[request_fields["outputFormat"]]
    intent = {
        intent_fields["generationRequestIdentity"]: request_id,
        intent_fields["prompt"]: resolved_prompt,
        intent_fields["imageCount"]: test_contract["defaultImageCount"],
        intent_fields["primaryOutputIndex"]: test_contract[
            "defaultPrimaryOutputIndex"
        ],
        intent_fields["imageSize"]: generation_request[
            request_fields["imageSize"]
        ],
        intent_fields["outputFormat"]: output_format,
    }
    package = {
        "requestId": request_id,
        "prompt": resolved_prompt,
        "runtimeSemantics": copy.deepcopy(template["runtimeSemantics"]),
        "templateJsonSha256": template_sha,
        "referenceImageSha256": reference_sha,
        "output": {
            "imageCount": test_contract["defaultImageCount"],
            "imageSize": generation_request[request_fields["imageSize"]],
            "outputFormat": output_format,
        },
    }
    task = {
        task_fields["artifactType"]: execution["artifactTypes"]["task"],
        task_fields["schemaVersion"]: rules["schemaVersion"],
        task_fields["taskIdentity"]: request_id,
        task_fields["revision"]: 1,
        task_fields["sourceImageSha256"]: reference_sha,
        task_fields["generationPackageSha256"]: _sha_bytes(_json_bytes(package)),
        task_fields["productionPinSha256"]: pin_sha,
        task_fields["inputSha256"]: case_sha,
        task_fields["requestIntent"]: intent,
        task_fields["requestIntentSha256"]: _sha_bytes(_canonical_bytes(intent)),
    }
    return generation_request, package, task, case_sha


def _case_generation(
    *,
    case: dict[str, Any],
    resolved_prompt: str,
    template: dict[str, Any],
    template_sha: str,
    pin_sha: str,
    reference_path: Path,
    reference_sha: str,
    case_dir: Path,
    adapters: Any,
    rules: dict[str, Any],
    timestamp: str,
    expected_submission_sha: str | None = None,
    record_submission_sha: Callable[[str], None] | None = None,
    expected_wal_binding: dict[str, Any] | None = None,
    record_wal_binding: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], bool]:
    test_contract = rules["templateTestContract"]
    execution = rules["generationExecutionContract"]
    case_fields = test_contract["caseFields"]
    request_fields = test_contract["generationRequestFields"]
    task_fields = execution["taskFields"]
    intent_fields = execution["requestIntentFields"]
    wal_fields = execution["walFields"]
    submission_fields = execution["submissionFields"]
    poll_fields = execution["pollResultFields"]
    review_fields = test_contract["reviewFields"]
    names = test_contract["artifactNames"]
    case_id = case[case_fields["caseIdentity"]]
    if case_dir.is_symlink() or (
        case_dir.exists() and case_dir.resolve() != case_dir
    ):
        raise ValueError("template test case directory is unsafe")
    generation_request, package, task, case_sha = _case_execution_facts(
        case=case,
        resolved_prompt=resolved_prompt,
        template=template,
        template_sha=template_sha,
        pin_sha=pin_sha,
        reference_sha=reference_sha,
        rules=rules,
    )
    request_id = generation_request[request_fields["requestIdentity"]]
    output_format_role = test_contract["defaultOutputFormatRole"]
    output_format = execution["outputFormats"][output_format_role]
    task_path = case_dir / names["task"]
    wal_path = case_dir / names["wal"]
    submission_path = case_dir / names["submission"]
    review_path = case_dir / names["review"]
    task_preexisting = task_path.exists()
    if not task_preexisting and (
        expected_wal_binding is not None or expected_submission_sha is not None
    ):
        raise TemplateTestIntegrityError("frozen generation task is missing")
    try:
        _write_new_or_same(task_path, _json_bytes(task))
    except ValueError as exc:
        if task_preexisting:
            raise TemplateTestIntegrityError(
                "frozen generation task changed"
            ) from exc
        raise

    def wal_binding(value: dict[str, Any]) -> dict[str, Any]:
        binding_fields = test_contract["walBindingFields"]
        return {
            binding_fields["sha256"]: _sha_bytes(_json_bytes(value)),
            binding_fields["status"]: value[wal_fields["status"]],
            binding_fields["pollAttemptCount"]: value[
                wal_fields["pollAttemptCount"]
            ],
            binding_fields["failureClass"]: value[wal_fields["failureClass"]],
        }

    def persist_wal(value: dict[str, Any]) -> None:
        _write_mutable(wal_path, value)
        if record_wal_binding is not None:
            record_wal_binding(wal_binding(value))

    if wal_path.exists():
        if wal_path.is_symlink() or not wal_path.is_file():
            raise TemplateTestIntegrityError("generation WAL path is unsafe")
        try:
            wal = _load_json(wal_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise TemplateTestIntegrityError("generation WAL is unreadable") from exc
        if not _wal_semantics_valid(wal, task, execution):
            raise TemplateTestIntegrityError(
                "generation WAL does not match the frozen task"
            )
        current_binding = wal_binding(wal)
        if expected_wal_binding is None:
            if wal[wal_fields["status"]] != execution["walStatuses"]["prepared"]:
                raise TemplateTestIntegrityError(
                    "unbound generation WAL is not prepared"
                )
            if record_wal_binding is not None:
                record_wal_binding(current_binding)
        elif current_binding != expected_wal_binding:
            binding_fields = test_contract["walBindingFields"]
            expected_sha = expected_wal_binding.get(binding_fields["sha256"])
            old_status = expected_wal_binding.get(binding_fields["status"])
            old_count = expected_wal_binding.get(
                binding_fields["pollAttemptCount"]
            )
            old_failure = expected_wal_binding.get(binding_fields["failureClass"])
            new_status = current_binding[binding_fields["status"]]
            new_count = current_binding[binding_fields["pollAttemptCount"]]
            new_failure = current_binding[binding_fields["failureClass"]]
            statuses = execution["walStatuses"]
            failures = execution["failureClasses"]
            allowed_forward = bool(
                wal.get(wal_fields["previousWalSha256"]) == expected_sha
                and (
                    (
                        old_status == statuses["prepared"]
                        and new_status in {statuses["submitted"], statuses["failed"]}
                        and new_count == old_count
                    )
                    or (
                        old_status == statuses["submitted"]
                        and (
                            (new_status == statuses["submitted"] and new_count == old_count + 1)
                            or (
                                new_status in {statuses["succeeded"], statuses["failed"]}
                                and new_count == old_count
                            )
                        )
                    )
                    or (
                        old_status == statuses["failed"]
                        and old_failure == failures["retryable"]
                        and (
                            (new_status == statuses["submitted"] and new_count == old_count)
                            or (
                                new_status == statuses["failed"]
                                and new_failure == failures["permanent"]
                                and new_count == old_count
                            )
                        )
                    )
                )
            )
            if not allowed_forward:
                raise TemplateTestIntegrityError(
                    "generation WAL does not continue the manifest binding"
                )
            if record_wal_binding is not None:
                record_wal_binding(current_binding)
        resumed = True
    else:
        wal = _prepared_wal(task, rules, timestamp)
        persist_wal(wal)
        resumed = False
    status = wal.get(wal_fields["status"])

    def valid_submission(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != set(
            submission_fields.values()
        ):
            return False
        submission_status = value.get(submission_fields["status"])
        provider = value.get(submission_fields["provider"])
        model = value.get(submission_fields["model"])
        request_identity = value.get(
            submission_fields["providerRequestIdentity"]
        )
        if not (
            submission_status in execution["submissionStatuses"].values()
            and _execution_identity_valid(
                provider, execution["providerIdentityPattern"]
            )
            and _execution_identity_valid(model, execution["modelIdentityPattern"])
        ):
            return False
        if submission_status == execution["submissionStatuses"]["submitted"]:
            return bool(
                _execution_identity_valid(
                    request_identity,
                    execution["opaqueExecutionIdentityPattern"],
                )
                and value.get(submission_fields["failureClass"]) is None
                and value.get(submission_fields["failureReason"]) is None
            )
        return bool(
            request_identity is None
            and value.get(submission_fields["failureClass"])
            in execution["failureClasses"].values()
            and isinstance(value.get(submission_fields["failureReason"]), str)
            and re.fullmatch(
                re.escape(execution["persistedErrorSanitization"]["digestPrefix"])
                + r"[0-9a-f]{"
                + str(execution["persistedErrorSanitization"]["digestLength"])
                + r"}",
                value[submission_fields["failureReason"]],
            )
        )

    frozen_submission: dict[str, Any] | None = None
    if submission_path.exists():
        if submission_path.is_symlink() or not submission_path.is_file():
            raise TemplateTestIntegrityError(
                "generation submission evidence path is unsafe"
            )
        try:
            candidate_submission = _load_json(submission_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise TemplateTestIntegrityError(
                "generation submission evidence is unreadable"
            ) from exc
        if not valid_submission(candidate_submission):
            raise TemplateTestIntegrityError(
                "generation submission evidence shape invalid"
            )
        frozen_submission = candidate_submission

    if frozen_submission is not None:
        frozen_submission_sha = _sha_bytes(_json_bytes(frozen_submission))
        if (
            expected_submission_sha is not None
            and expected_submission_sha != frozen_submission_sha
        ):
            raise TemplateTestIntegrityError(
                "generation submission digest does not match manifest"
            )
        if expected_submission_sha is None:
            raise TemplateTestNeedsInput(
                "unbound generation submission evidence requires reconciliation"
            )

    if status == execution["walStatuses"]["prepared"]:
        if frozen_submission is not None:
            submission = frozen_submission
        else:
            submit = getattr(adapters, "submit_generation", None)
            if not callable(submit):
                raise RuntimeError("generation adapter has no submit seam")
            submission = _call_with_image_snapshot(
                reference_path, submit, package, task
            )
            if (
                isinstance(submission, dict)
                and submission.get(submission_fields["status"])
                == execution["submissionStatuses"]["failed"]
            ):
                submission = copy.deepcopy(submission)
                submission[submission_fields["failureReason"]] = (
                    _safe_failure_reason(
                        submission.get(submission_fields["failureReason"]),
                        execution,
                    )
                )
            if not valid_submission(submission):
                raise RuntimeError("generation submission shape invalid")
            _write_new_or_same(submission_path, _json_bytes(submission))
            frozen_submission_sha = _sha_bytes(_json_bytes(submission))
            if expected_submission_sha is not None:
                if expected_submission_sha != frozen_submission_sha:
                    raise TemplateTestIntegrityError(
                        "generation submission digest does not match manifest"
                    )
            elif record_submission_sha is not None:
                record_submission_sha(frozen_submission_sha)
        previous = _sha_bytes(_json_bytes(wal))
        submission_status = submission[submission_fields["status"]]
        failure_class = submission[submission_fields["failureClass"]]
        if submission_status == execution["submissionStatuses"]["failed"]:
            if failure_class == execution["failureClasses"]["retryable"]:
                failure_class = execution["failureClasses"]["submissionUnknown"]
            wal.update(
                {
                    wal_fields["previousWalSha256"]: previous,
                    wal_fields["status"]: execution["walStatuses"]["failed"],
                    wal_fields["provider"]: submission[submission_fields["provider"]],
                    wal_fields["model"]: submission[submission_fields["model"]],
                    wal_fields["providerRequestIdentity"]: None,
                    wal_fields["failureClass"]: failure_class,
                    wal_fields["failureReason"]: "provider submission state requires reconciliation",
                    wal_fields["updatedAt"]: timestamp,
                }
            )
            persist_wal(wal)
            if failure_class == execution["failureClasses"]["submissionUnknown"]:
                raise TemplateTestNeedsInput(
                    "provider submission state is uncertain"
                )
            if failure_class == execution["failureClasses"]["permanent"]:
                raise TemplateTestPermanentGenerationFailure(
                    "generation submission permanently failed"
                )
            raise TemplateTestRoutedGenerationStop(failure_class)
        wal.update(
            {
                wal_fields["previousWalSha256"]: previous,
                wal_fields["status"]: execution["walStatuses"]["submitted"],
                wal_fields["provider"]: submission[submission_fields["provider"]],
                wal_fields["model"]: submission[submission_fields["model"]],
                wal_fields["providerRequestIdentity"]: submission[
                    submission_fields["providerRequestIdentity"]
                ],
                wal_fields["updatedAt"]: timestamp,
            }
        )
        persist_wal(wal)
        status = wal[wal_fields["status"]]
    elif status in {
        execution["walStatuses"]["submitted"],
        execution["walStatuses"]["succeeded"],
    }:
        if not (
            frozen_submission is not None
            and frozen_submission[submission_fields["status"]]
            == execution["submissionStatuses"]["submitted"]
            and wal.get(wal_fields["provider"])
            == frozen_submission[submission_fields["provider"]]
            and wal.get(wal_fields["model"])
            == frozen_submission[submission_fields["model"]]
            and wal.get(wal_fields["providerRequestIdentity"])
            == frozen_submission[
                submission_fields["providerRequestIdentity"]
            ]
        ):
            raise TemplateTestIntegrityError(
                "generation WAL does not match immutable submission evidence"
            )
    elif status == execution["walStatuses"]["failed"]:
        if not (
            frozen_submission is not None
            and wal.get(wal_fields["provider"])
            == frozen_submission[submission_fields["provider"]]
            and wal.get(wal_fields["model"])
            == frozen_submission[submission_fields["model"]]
            and wal.get(wal_fields["failureClass"])
            in execution["failureClasses"].values()
        ):
            raise TemplateTestIntegrityError(
                "failed generation WAL does not match submission evidence"
            )
        if wal[wal_fields["failureClass"]] == execution["failureClasses"][
            "submissionUnknown"
        ]:
            raise TemplateTestNeedsInput("provider submission state is uncertain")
        if wal[wal_fields["failureClass"]] == execution["failureClasses"][
            "retryable"
        ]:
            if wal[wal_fields["pollAttemptCount"]] >= execution["retryBudgets"][
                "retryable"
            ]:
                wal[wal_fields["failureClass"]] = execution["failureClasses"][
                    "permanent"
                ]
                wal[wal_fields["failureReason"]] = "generation retry budget exhausted"
                persist_wal(wal)
                raise TemplateTestPermanentGenerationFailure(
                    "generation retry budget exhausted"
                )
            previous = _sha_bytes(_json_bytes(wal))
            wal.update(
                {
                    wal_fields["previousWalSha256"]: previous,
                    wal_fields["status"]: execution["walStatuses"]["submitted"],
                    wal_fields["failureClass"]: None,
                    wal_fields["failureReason"]: None,
                    wal_fields["updatedAt"]: timestamp,
                }
            )
            persist_wal(wal)
            status = wal[wal_fields["status"]]
        elif wal[wal_fields["failureClass"]] == execution["failureClasses"][
            "permanent"
        ]:
            raise TemplateTestPermanentGenerationFailure(
                "generation submission is terminally failed"
            )
        else:
            raise TemplateTestRoutedGenerationStop(
                wal[wal_fields["failureClass"]]
            )
    extension = execution["outputFormatExtensions"][output_format_role]
    candidate_path = case_dir / f"{names['candidateStem']}{extension}"
    preexisting_candidates = list(case_dir.glob(f"{names['candidateStem']}.*"))
    if any(
        path != candidate_path or path.is_symlink() or not path.is_file()
        for path in preexisting_candidates
    ):
        raise TemplateTestIntegrityError(
            "generation candidate artifact set contains an unexpected path"
        )
    if status == execution["walStatuses"]["succeeded"]:
        candidate_set = list(case_dir.glob(f"{names['candidateStem']}.*"))
        if not (
            len(candidate_set) == 1
            and candidate_set[0] == candidate_path
            and candidate_path.is_file()
            and not candidate_path.is_symlink()
            and _sha_bytes(candidate_path.read_bytes())
            == wal.get(wal_fields["outputSha256"])
            and image_bytes_match_output_format(
                candidate_path.read_bytes(), output_format, execution
            )
        ):
            raise TemplateTestIntegrityError(
                "succeeded generation candidate is missing or changed"
            )
    elif status == execution["walStatuses"]["submitted"]:
        retry_budget = execution["retryBudgets"]["retryable"]
        attempt = wal.get(wal_fields["pollAttemptCount"])
        if not isinstance(attempt, int) or attempt >= retry_budget:
            previous = _sha_bytes(_json_bytes(wal))
            wal.update(
                {
                    wal_fields["previousWalSha256"]: previous,
                    wal_fields["status"]: execution["walStatuses"]["failed"],
                    wal_fields["failureClass"]: execution["failureClasses"][
                        "permanent"
                    ],
                    wal_fields["failureReason"]: "generation retry budget exhausted",
                    wal_fields["updatedAt"]: timestamp,
                }
            )
            persist_wal(wal)
            raise TemplateTestPermanentGenerationFailure(
                "generation polling budget exhausted"
            )
        previous = _sha_bytes(_json_bytes(wal))
        wal[wal_fields["previousWalSha256"]] = previous
        wal[wal_fields["pollAttemptCount"]] = attempt + 1
        wal[wal_fields["updatedAt"]] = timestamp
        persist_wal(wal)
        poll = getattr(adapters, "poll_generation", None)
        if not callable(poll):
            raise RuntimeError("generation adapter has no poll seam")
        submission = {
            submission_fields["status"]: execution["submissionStatuses"][
                "submitted"
            ],
            submission_fields["provider"]: wal[wal_fields["provider"]],
            submission_fields["model"]: wal[wal_fields["model"]],
            submission_fields["providerRequestIdentity"]: wal[
                wal_fields["providerRequestIdentity"]
            ],
            submission_fields["failureClass"]: None,
            submission_fields["failureReason"]: None,
        }
        poll_result = _call_with_image_snapshot(
            reference_path, poll, package, task, submission
        )
        if not isinstance(poll_result, dict) or set(poll_result) != set(
            poll_fields.values()
        ):
            raise RuntimeError("generation poll shape invalid")
        if poll_result.get(poll_fields["status"]) != execution[
            "pollStatuses"
        ]["succeeded"]:
            failure_class = poll_result.get(poll_fields["failureClass"])
            failure_reason = poll_result.get(poll_fields["failureReason"])
            if not (
                failure_class in execution["failureClasses"].values()
                and isinstance(failure_reason, str)
                and failure_reason.strip()
            ):
                raise RuntimeError("generation poll failure shape invalid")
            if (
                failure_class == execution["failureClasses"]["retryable"]
                and wal[wal_fields["pollAttemptCount"]] >= retry_budget
            ):
                failure_class = execution["failureClasses"]["permanent"]
                failure_reason = "generation retry budget exhausted"
            previous = _sha_bytes(_json_bytes(wal))
            wal.update(
                {
                    wal_fields["previousWalSha256"]: previous,
                    wal_fields["status"]: execution["walStatuses"]["failed"],
                    wal_fields["failureClass"]: failure_class,
                    wal_fields["failureReason"]: f"provider-{failure_class}",
                    wal_fields["updatedAt"]: timestamp,
                }
            )
            persist_wal(wal)
            if failure_class == execution["failureClasses"]["retryable"]:
                raise RetryableTemplateTestGeneration(
                    "generation poll is retryable"
                )
            if failure_class == execution["failureClasses"]["permanent"]:
                raise TemplateTestPermanentGenerationFailure(
                    "generation poll permanently failed"
                )
            raise TemplateTestRoutedGenerationStop(failure_class)
        payload = poll_result.get(poll_fields["imageBytes"])
        if not image_bytes_match_output_format(payload, output_format, execution):
            raise RuntimeError("generation output image invalid")
        if poll_result.get(poll_fields["extension"]) != extension:
            raise RuntimeError("generation output extension mismatch")
        output_assets = poll_result.get(poll_fields["outputAssets"])
        asset_fields = execution["outputAssetFields"]
        if not (
            isinstance(output_assets, list)
            and len(output_assets) == test_contract["defaultImageCount"]
            and all(
                isinstance(item, dict)
                and set(item) == set(asset_fields.values())
                and _execution_identity_valid(
                    item.get(asset_fields["providerOutputIdentity"]),
                    execution["opaqueExecutionIdentityPattern"],
                )
                and item.get(asset_fields["sha256"]) == _sha_bytes(payload)
                for item in output_assets
            )
            and len(
                {
                    item[asset_fields["providerOutputIdentity"]]
                    for item in output_assets
                }
            )
            == len(output_assets)
            and poll_result.get(poll_fields["providerOutputIdentity"])
            == output_assets[test_contract["defaultPrimaryOutputIndex"]][
                asset_fields["providerOutputIdentity"]
            ]
        ):
            raise RuntimeError("generation output assets invalid")
        candidate_preexisting = candidate_path.exists()
        try:
            _write_new_or_same(candidate_path, payload)
        except ValueError as exc:
            if candidate_preexisting:
                raise TemplateTestIntegrityError(
                    "preexisting generation candidate conflicts with provider output"
                ) from exc
            raise
        previous = _sha_bytes(_json_bytes(wal))
        wal.update(
            {
                wal_fields["previousWalSha256"]: previous,
                wal_fields["status"]: execution["walStatuses"]["succeeded"],
                wal_fields["providerOutputIdentity"]: poll_result[
                    poll_fields["providerOutputIdentity"]
                ],
                wal_fields["outputSha256"]: _sha_bytes(payload),
                wal_fields["outputAssets"]: poll_result[
                    poll_fields["outputAssets"]
                ],
                wal_fields["updatedAt"]: timestamp,
            }
        )
        persist_wal(wal)
    image_sha = _sha_bytes(candidate_path.read_bytes())
    review_request = {
        review_fields["templateJsonSha256"]: template_sha,
        review_fields["testCaseSha256"]: case_sha,
        review_fields["generatedImageSha256"]: image_sha,
    }
    review_preexisting = review_path.exists()
    if review_preexisting:
        if review_path.is_symlink():
            raise TemplateTestIntegrityError("template test review is unsafe")
        try:
            review = _load_json(review_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise TemplateTestIntegrityError(
                "frozen template test review is unreadable"
            ) from exc
    else:
        inspect = getattr(adapters, "inspect_template_test", None)
        if not callable(inspect):
            raise RuntimeError("generation adapter has no T1 review seam")
        review = _call_with_image_snapshot(
            candidate_path, inspect, review_request
        )
        if not _review_evidence_valid(review, review_request, test_contract):
            raise TemplateTestReviewEvidenceInvalid(
                "template test review evidence invalid"
            )
        _write_new_or_same(review_path, _json_bytes(review))
    if not _review_evidence_valid(review, review_request, test_contract):
        if review_preexisting:
            raise TemplateTestIntegrityError(
                "frozen template test review evidence invalid"
            )
        raise TemplateTestReviewEvidenceInvalid(
            "template test review evidence invalid"
        )
    return {
        test_contract["caseReportFields"]["generationRequest"]: generation_request,
        test_contract["caseReportFields"]["outputImagePath"]: str(candidate_path),
        test_contract["caseReportFields"]["outputImageSha256"]: image_sha,
        test_contract["caseReportFields"]["visibleDeviations"]: review[
            review_fields["visibleDeviations"]
        ],
        test_contract["caseReportFields"]["reviewPass"]: review[
            review_fields["pass"]
        ],
    }, resumed


def _completed_cases_replay_valid(
    *,
    normalized_cases: list[dict[str, Any]],
    prompts: list[tuple[str, dict[str, Any]]],
    report_cases: Any,
    template: dict[str, Any],
    template_sha: str,
    pin_sha: str,
    reference_path: Path,
    reference_sha: str,
    output_dir: Path,
    adapters: Any,
    rules: dict[str, Any],
    timestamp: str,
    submission_sha_by_case: Any,
    wal_bindings_by_case: Any,
) -> bool:
    contract = rules["templateTestContract"]
    case_fields = contract["caseFields"]
    report_fields = contract["caseReportFields"]
    names = contract["artifactNames"]
    case_ids = [case[case_fields["caseIdentity"]] for case in normalized_cases]
    if not (
        isinstance(report_cases, list)
        and len(report_cases) == len(normalized_cases)
        and isinstance(submission_sha_by_case, dict)
        and set(case_ids) <= set(submission_sha_by_case)
        and isinstance(wal_bindings_by_case, dict)
        and set(case_ids) <= set(wal_bindings_by_case)
        and all(
            isinstance(submission_sha_by_case[case_id], str)
            and re.fullmatch(r"[0-9a-f]{64}", submission_sha_by_case[case_id])
            for case_id in case_ids
        )
    ):
        return False
    for case, (resolved_prompt, user_input), recorded in zip(
        normalized_cases, prompts, report_cases, strict=True
    ):
        case_id = case[case_fields["caseIdentity"]]
        case_dir = output_dir / f"case-{case_id}"
        required = [
            case_dir / names["task"],
            case_dir / names["wal"],
            case_dir / names["submission"],
            case_dir / names["review"],
        ]
        candidates = list(case_dir.glob(f"{names['candidateStem']}.*"))
        if (
            case_dir.is_symlink()
            or any(
                not path.is_file() or path.is_symlink() for path in required
            )
            or len(candidates) != 1
            or candidates[0].is_symlink()
        ):
            return False
        try:
            generation, _resumed = _case_generation(
                case=case,
                resolved_prompt=resolved_prompt,
                template=template,
                template_sha=template_sha,
                pin_sha=pin_sha,
                reference_path=reference_path,
                reference_sha=reference_sha,
                case_dir=case_dir,
                adapters=adapters,
                rules=rules,
                timestamp=timestamp,
                expected_submission_sha=submission_sha_by_case[case_id],
                expected_wal_binding=wal_bindings_by_case[case_id],
            )
        except (OSError, TypeError, ValueError, KeyError, RuntimeError):
            return False
        expected = {
            report_fields["caseIdentity"]: case_id,
            report_fields["mode"]: case[case_fields["mode"]],
            report_fields["userInput"]: user_input,
            report_fields["resolvedPrompt"]: resolved_prompt,
            **generation,
            report_fields["outcome"]: "completed",
            report_fields["errorCode"]: None,
            report_fields["message"]: None,
        }
        if recorded != expected:
            return False
    return True


def _failed_case_lineage_valid(
    *,
    case: dict[str, Any],
    resolved_prompt: str,
    report_error_code: str,
    template: dict[str, Any],
    template_sha: str,
    pin_sha: str,
    reference_path: Path | None,
    reference_sha: str | None,
    output_dir: Path,
    submission_sha_by_case: dict[str, Any],
    wal_bindings_by_case: dict[str, Any],
    rules: dict[str, Any],
) -> bool:
    contract = rules["templateTestContract"]
    execution = rules["generationExecutionContract"]
    case_fields = contract["caseFields"]
    names = contract["artifactNames"]
    case_id = case[case_fields["caseIdentity"]]
    case_dir = output_dir / f"case-{case_id}"
    task_path = case_dir / names["task"]
    wal_path = case_dir / names["wal"]
    submission_path = case_dir / names["submission"]
    review_path = case_dir / names["review"]
    has_wal_binding = case_id in wal_bindings_by_case
    has_submission_binding = case_id in submission_sha_by_case
    if not has_wal_binding:
        return bool(
            not has_submission_binding
            and not task_path.exists()
            and not wal_path.exists()
            and not submission_path.exists()
        )
    if reference_path is None or reference_sha is None:
        return False
    try:
        _generation_request, _package, expected_task, _case_sha = (
            _case_execution_facts(
                case=case,
                resolved_prompt=resolved_prompt,
                template=template,
                template_sha=template_sha,
                pin_sha=pin_sha,
                reference_sha=reference_sha,
                rules=rules,
            )
        )
        if not (
            task_path.is_file()
            and not task_path.is_symlink()
            and task_path.read_bytes() == _json_bytes(expected_task)
            and wal_path.is_file()
            and not wal_path.is_symlink()
        ):
            return False
        wal = _load_json(wal_path)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return False
    binding_fields = contract["walBindingFields"]
    wal_fields = execution["walFields"]
    current_binding = {
        binding_fields["sha256"]: _sha_bytes(_json_bytes(wal)),
        binding_fields["status"]: wal.get(wal_fields["status"]),
        binding_fields["pollAttemptCount"]: wal.get(
            wal_fields["pollAttemptCount"]
        ),
        binding_fields["failureClass"]: wal.get(wal_fields["failureClass"]),
    }
    if not (
        _wal_semantics_valid(wal, expected_task, execution)
        and current_binding == wal_bindings_by_case[case_id]
    ):
        return False
    if has_submission_binding:
        try:
            submission = _load_json(submission_path)
        except (OSError, json.JSONDecodeError):
            return False
        submission_fields = execution["submissionFields"]
        if not (
            isinstance(submission, dict)
            and set(submission) == set(submission_fields.values())
            and not submission_path.is_symlink()
            and _sha_bytes(_json_bytes(submission))
            == submission_sha_by_case[case_id]
            and submission.get(submission_fields["provider"])
            == wal.get(wal_fields["provider"])
            and submission.get(submission_fields["model"])
            == wal.get(wal_fields["model"])
            and (
                submission.get(submission_fields["providerRequestIdentity"])
                == wal.get(wal_fields["providerRequestIdentity"])
                or wal.get(wal_fields["status"])
                == execution["walStatuses"]["failed"]
            )
        ):
            return False
    elif not (
        wal.get(wal_fields["status"]) == execution["walStatuses"]["prepared"]
        and not submission_path.exists()
    ):
        return False
    if report_error_code == contract["errorCodes"]["generationPermanent"]:
        return bool(
            wal.get(wal_fields["status"]) == execution["walStatuses"]["failed"]
            and wal.get(wal_fields["failureClass"])
            == execution["failureClasses"]["permanent"]
        )
    if wal.get(wal_fields["status"]) == execution["walStatuses"]["succeeded"]:
        task_fields = execution["taskFields"]
        intent_fields = execution["requestIntentFields"]
        intent = expected_task[task_fields["requestIntent"]]
        output_format = intent[intent_fields["outputFormat"]]
        output_format_role = next(
            (
                role
                for role, value in execution["outputFormats"].items()
                if value == output_format
            ),
            None,
        )
        if output_format_role is None:
            return False
        candidate_path = case_dir / (
            names["candidateStem"]
            + execution["outputFormatExtensions"][output_format_role]
        )
        candidate_set = list(case_dir.glob(f"{names['candidateStem']}.*"))
        if not (
            len(candidate_set) == 1
            and candidate_set[0] == candidate_path
            and candidate_path.is_file()
            and not candidate_path.is_symlink()
            and _sha_bytes(candidate_path.read_bytes())
            == wal.get(wal_fields["outputSha256"])
            and image_bytes_match_output_format(
                candidate_path.read_bytes(), output_format, execution
            )
        ):
            return False
        if review_path.exists():
            return False
    return report_error_code == contract["errorCodes"]["externalFailure"]


def _build_report(
    *,
    rules: dict[str, Any],
    contract: dict[str, Any],
    invocation_id: str,
    template: dict[str, Any],
    template_path: Path,
    template_sha: str,
    template_revision: int,
    pin: dict[str, Any],
    request_sha: str,
    cases: list[dict[str, Any]],
    timestamp: str,
    outcome: str,
    error_code: str | None,
) -> dict[str, Any]:
    fields = contract["reportFields"]
    release_contract = rules["releaseManagementContract"]
    pin_fields = release_contract["productionPinFields"]
    skill_fields = release_contract["productionPinSkillFields"]
    return {
        fields["artifactType"]: contract["artifactTypes"]["report"],
        fields["schemaVersion"]: rules["schemaVersion"],
        fields["invocationIdentity"]: invocation_id,
        fields["templateKey"]: template["key"],
        fields["templateRevision"]: template_revision,
        fields["templateJsonPath"]: str(template_path),
        fields["templateJsonSha256"]: template_sha,
        fields["templateImageUrl"]: template["referenceImage"],
        fields["testerVersion"]: pin[pin_fields["skill"]][
            skill_fields["version"]
        ],
        fields["productionPin"]: pin,
        fields["requestSha256"]: request_sha,
        fields["cases"]: cases,
        fields["startedAt"]: timestamp,
        fields["completedAt"]: timestamp,
        fields["outcome"]: outcome,
        fields["errorCode"]: error_code,
    }


def run_template_test(
    request: Any,
    output_root: str | Path,
    adapters: Any,
    *,
    clock: Callable[[], datetime] | None = None,
) -> TemplateTestResult:
    rules = _rules()
    contract = rules["templateTestContract"]
    request_fields = contract["requestFields"]
    case_fields = contract["caseFields"]
    manifest_fields = contract["manifestFields"]
    report_fields = contract["reportFields"]
    case_report_fields = contract["caseReportFields"]
    names = contract["artifactNames"]
    errors = contract["errorCodes"]
    states = contract["states"]
    timestamp = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")
    raw_invocation = (
        request.get(request_fields["invocationIdentity"])
        if isinstance(request, dict)
        else None
    )
    invocation_id = raw_invocation if isinstance(raw_invocation, str) else "invalid-t1"
    output_root_path = Path(output_root).resolve()
    try:
        raw_template_path = request[request_fields["templateJsonPath"]]
        if not isinstance(raw_template_path, str):
            raise ValueError("template path must be a string")
        lexical_template_path = Path(raw_template_path)
        if lexical_template_path.is_symlink():
            raise ValueError("template path cannot be a symlink")
        template_path = lexical_template_path.resolve()
        normalized_request = _normalized_request(request, template_path, contract)
        invocation_id = normalized_request[request_fields["invocationIdentity"]]
    except (KeyError, OSError, TypeError, ValueError):
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_root_path,
            error_code=errors["invalidRequest"],
            message="T1 请求字段或值无效。",
        )
    production_workspace = template_path.parent.resolve()
    if (
        output_root_path == production_workspace
        or output_root_path.is_relative_to(production_workspace)
        or production_workspace.is_relative_to(output_root_path)
    ):
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_root_path,
            error_code=errors["invalidRequest"],
            message="T1 输出根目录必须与正式模板生产目录完全隔离。",
        )
    output_dir = _safe_output_dir(output_root_path, invocation_id)
    if output_dir is None:
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_root_path,
            error_code=errors["invalidRequest"],
            message="T1 输出目录越界或包含符号链接。",
        )
    if template_path.is_relative_to(output_dir):
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_dir,
            error_code=errors["invalidRequest"],
            message="正式模板 JSON 不能位于 T1 输出目录内。",
        )
    if not template_path.is_file() or template_path.is_symlink():
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_dir,
            error_code=errors["invalidTemplate"],
            message="指定的正式模板 JSON 不存在或不是普通文件。",
        )
    try:
        template_bytes = template_path.read_bytes()
        template = json.loads(template_bytes)
    except (OSError, json.JSONDecodeError):
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_dir,
            error_code=errors["invalidTemplate"],
            message="指定的正式模板 JSON 无法读取。",
        )
    template_errors = _formal_template_errors(template)
    if template_errors:
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_dir,
            error_code=errors["invalidTemplate"],
            message="指定的正式模板 JSON 未通过静态合同。",
        )
    try:
        prompts = []
        top_level = rules["formalProjection"]["topLevel"]
        for case in normalized_request[request_fields["cases"]]:
            resolved_prompt, user_input = _case_prompt(
                template, case, contract, rules
            )
            prompts.append(
                (
                    _compile_actual_prompt(
                        resolved_prompt,
                        template[top_level["runtimeSemantics"]],
                        rules,
                    ),
                    user_input,
                )
            )
        if any(
            len(prompt) > contract["maximumPromptLength"]
            for prompt, _user_input in prompts
        ):
            raise ValueError("resolved prompt is too long")
        pin = runtime_production_pin(REPO_ROOT)
    except (OSError, TypeError, ValueError, KeyError):
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_dir,
            error_code=errors["invalidRequest"],
            message="T1 编辑输入无法编译或运行版本无效。",
        )
    template_sha = _sha_bytes(template_bytes)
    request_sha = _sha_bytes(_canonical_bytes(normalized_request))
    pin_sha = _sha_bytes(_json_bytes(pin))
    preexisting_entries = list(output_dir.iterdir()) if output_dir.exists() else []
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / names["manifest"]
    report_path = output_dir / names["report"]
    pin_path = output_dir / names["pin"]
    request_path = output_dir / names["request"]
    resumed = manifest_path.exists()
    if resumed:
        try:
            manifest = None if manifest_path.is_symlink() else _load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            manifest = None
        if not (
            isinstance(manifest, dict)
            and set(manifest) == set(manifest_fields.values())
            and manifest.get(manifest_fields["artifactType"])
            == contract["artifactTypes"]["manifest"]
            and manifest.get(manifest_fields["schemaVersion"])
            == rules["schemaVersion"]
            and manifest.get(manifest_fields["invocationIdentity"])
            == invocation_id
            and manifest.get(manifest_fields["state"]) in states.values()
            and manifest.get(manifest_fields["requestSha256"]) == request_sha
            and manifest.get(manifest_fields["templateJsonSha256"])
            == template_sha
            and manifest.get(manifest_fields["productionPinSha256"])
            == pin_sha
            and manifest.get(manifest_fields["caseIdentities"])
            == [
                case[case_fields["caseIdentity"]]
                for case in normalized_request[request_fields["cases"]]
            ]
            and isinstance(
                manifest.get(manifest_fields["caseSubmissionSha256"]), dict
            )
            and set(manifest[manifest_fields["caseSubmissionSha256"]])
            <= set(manifest[manifest_fields["caseIdentities"]])
            and all(
                isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in manifest[
                    manifest_fields["caseSubmissionSha256"]
                ].values()
            )
            and isinstance(manifest.get(manifest_fields["caseWalBindings"]), dict)
            and set(manifest[manifest_fields["caseWalBindings"]])
            <= set(manifest[manifest_fields["caseIdentities"]])
            and all(
                isinstance(binding, dict)
                and set(binding) == set(contract["walBindingFields"].values())
                and isinstance(
                    binding[contract["walBindingFields"]["sha256"]], str
                )
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    binding[contract["walBindingFields"]["sha256"]],
                )
                and binding[contract["walBindingFields"]["status"]]
                in rules["generationExecutionContract"]["walStatuses"].values()
                and isinstance(
                    binding[
                        contract["walBindingFields"]["pollAttemptCount"]
                    ],
                    int,
                )
                and not isinstance(
                    binding[
                        contract["walBindingFields"]["pollAttemptCount"]
                    ],
                    bool,
                )
                and (
                    binding[contract["walBindingFields"]["failureClass"]]
                    is None
                    or binding[contract["walBindingFields"]["failureClass"]]
                    in rules["generationExecutionContract"]["failureClasses"].values()
                )
                for binding in manifest[
                    manifest_fields["caseWalBindings"]
                ].values()
            )
            and (
                manifest.get(manifest_fields["referenceImageSha256"]) is None
                or (
                    isinstance(
                        manifest[manifest_fields["referenceImageSha256"]], str
                    )
                    and re.fullmatch(
                        r"[0-9a-f]{64}",
                        manifest[manifest_fields["referenceImageSha256"]],
                    )
                )
            )
            and (
                manifest.get(manifest_fields["reportSha256"]) is None
                or (
                    isinstance(manifest[manifest_fields["reportSha256"]], str)
                    and re.fullmatch(
                        r"[0-9a-f]{64}",
                        manifest[manifest_fields["reportSha256"]],
                    )
                )
            )
            and isinstance(manifest.get(manifest_fields["updatedAt"]), str)
            and manifest[manifest_fields["updatedAt"]].strip()
        ):
            return TemplateTestResult(
                "blocked",
                invocation_id,
                states["blocked"],
                output_dir,
                error_code=errors["integrityFailure"],
                message="已有 T1 manifest 与当前请求不一致。",
                resumed=True,
            )
        if not (
            pin_path.is_file()
            and not pin_path.is_symlink()
            and pin_path.read_bytes() == _json_bytes(pin)
            and request_path.is_file()
            and not request_path.is_symlink()
            and request_path.read_bytes() == _json_bytes(normalized_request)
        ):
            return TemplateTestResult(
                "blocked",
                invocation_id,
                states["blocked"],
                output_dir,
                error_code=errors["integrityFailure"],
                message="T1 冻结请求或版本 pin 摘要不一致。",
                resumed=True,
            )
        if manifest.get(manifest_fields["state"]) == states["completed"]:
            try:
                report_payload = report_path.read_bytes()
            except OSError:
                report_payload = b""
            reference_candidates = list(
                output_dir.glob(
                    contract["artifactNames"]["referenceImageStem"] + ".*"
                )
            )
            try:
                report = json.loads(report_payload)
            except json.JSONDecodeError:
                report = None
            if not (
                not report_path.is_symlink()
                and _sha_bytes(report_payload)
                == manifest.get(manifest_fields["reportSha256"])
                and len(reference_candidates) == 1
                and reference_candidates[0].is_file()
                and not reference_candidates[0].is_symlink()
                and _sha_bytes(reference_candidates[0].read_bytes())
                == manifest.get(manifest_fields["referenceImageSha256"])
                and _completed_report_valid(
                    report,
                    output_dir=output_dir,
                    contract=contract,
                    invocation_id=invocation_id,
                    template=template,
                    template_sha=template_sha,
                    template_revision=normalized_request[
                        request_fields["templateRevision"]
                    ],
                    pin=pin,
                    request_sha=request_sha,
                    case_ids=manifest[manifest_fields["caseIdentities"]],
                    template_path=template_path,
                    rules=rules,
                    normalized_cases=normalized_request[request_fields["cases"]],
                    prompts=prompts,
                )
                and _completed_cases_replay_valid(
                    normalized_cases=normalized_request[request_fields["cases"]],
                    prompts=prompts,
                    report_cases=(
                        report.get(report_fields["cases"])
                        if isinstance(report, dict)
                        else None
                    ),
                    template=template,
                    template_sha=template_sha,
                    pin_sha=pin_sha,
                    reference_path=reference_candidates[0],
                    reference_sha=manifest[
                        manifest_fields["referenceImageSha256"]
                    ],
                    output_dir=output_dir,
                    adapters=adapters,
                    rules=rules,
                    timestamp=timestamp,
                    submission_sha_by_case=manifest[
                        manifest_fields["caseSubmissionSha256"]
                    ],
                    wal_bindings_by_case=manifest[
                        manifest_fields["caseWalBindings"]
                    ],
                )
            ):
                return TemplateTestResult(
                    "blocked",
                    invocation_id,
                    states["blocked"],
                    output_dir,
                    error_code=errors["integrityFailure"],
                    message="完成态 T1 报告摘要不一致。",
                    resumed=True,
                )
            return TemplateTestResult(
                "completed",
                invocation_id,
                states["completed"],
                output_dir,
                report_path,
                resumed=True,
            )
        if manifest.get(manifest_fields["state"]) == states["failed"]:
            try:
                report_payload = report_path.read_bytes()
                report = json.loads(report_payload)
            except (OSError, json.JSONDecodeError):
                report_payload = b""
                report = None
            failed_report_valid = bool(
                not report_path.is_symlink()
                and _sha_bytes(report_payload)
                == manifest.get(manifest_fields["reportSha256"])
                and _completed_report_valid(
                    report,
                    output_dir=output_dir,
                    contract=contract,
                    invocation_id=invocation_id,
                    template=template,
                    template_sha=template_sha,
                    template_revision=normalized_request[
                        request_fields["templateRevision"]
                    ],
                    pin=pin,
                    request_sha=request_sha,
                    case_ids=manifest[manifest_fields["caseIdentities"]],
                    template_path=template_path,
                    rules=rules,
                    normalized_cases=normalized_request[request_fields["cases"]],
                    prompts=prompts,
                )
            )
            completed_prefix = (
                report[report_fields["cases"]][:-1]
                if failed_report_valid and isinstance(report, dict)
                else []
            )
            reference_candidates = list(
                output_dir.glob(
                    contract["artifactNames"]["referenceImageStem"] + ".*"
                )
            )
            reference_path_for_replay = (
                reference_candidates[0]
                if len(reference_candidates) == 1
                and reference_candidates[0].is_file()
                and not reference_candidates[0].is_symlink()
                else None
            )
            reference_sha_for_replay = manifest.get(
                manifest_fields["referenceImageSha256"]
            )
            if failed_report_valid:
                failed_index = len(report[report_fields["cases"]]) - 1
                failed_report_valid = _failed_case_lineage_valid(
                    case=normalized_request[request_fields["cases"]][failed_index],
                    resolved_prompt=prompts[failed_index][0],
                    report_error_code=report[report_fields["errorCode"]],
                    template=template,
                    template_sha=template_sha,
                    pin_sha=pin_sha,
                    reference_path=reference_path_for_replay,
                    reference_sha=reference_sha_for_replay,
                    output_dir=output_dir,
                    submission_sha_by_case=manifest[
                        manifest_fields["caseSubmissionSha256"]
                    ],
                    wal_bindings_by_case=manifest[
                        manifest_fields["caseWalBindings"]
                    ],
                    rules=rules,
                )
            if failed_report_valid and completed_prefix:
                completed_count = len(completed_prefix)
                failed_report_valid = bool(
                    reference_path_for_replay is not None
                    and _completed_cases_replay_valid(
                        normalized_cases=normalized_request[
                            request_fields["cases"]
                        ][:completed_count],
                        prompts=prompts[:completed_count],
                        report_cases=completed_prefix,
                        template=template,
                        template_sha=template_sha,
                        pin_sha=pin_sha,
                        reference_path=reference_path_for_replay,
                        reference_sha=manifest[
                            manifest_fields["referenceImageSha256"]
                        ],
                        output_dir=output_dir,
                        adapters=adapters,
                        rules=rules,
                        timestamp=timestamp,
                        submission_sha_by_case=manifest[
                            manifest_fields["caseSubmissionSha256"]
                        ],
                        wal_bindings_by_case=manifest[
                            manifest_fields["caseWalBindings"]
                        ],
                    )
                )
            if failed_report_valid:
                return TemplateTestResult(
                    "failed",
                    invocation_id,
                    states["failed"],
                    output_dir,
                    report_path,
                    error_code=report[report_fields["errorCode"]],
                    message="T1 已冻结失败报告；使用新 invocation ID 重试。",
                    resumed=True,
                )
            return TemplateTestResult(
                "blocked",
                invocation_id,
                states["blocked"],
                output_dir,
                error_code=errors["integrityFailure"],
                message="失败态 T1 报告或上游 case 谱系不一致。",
                resumed=True,
            )
    else:
        if preexisting_entries:
            return TemplateTestResult(
                "blocked",
                invocation_id,
                states["blocked"],
                output_dir,
                error_code=errors["integrityFailure"],
                message="未跟踪的 T1 invocation 目录包含预置产物。",
            )
        manifest = {
            manifest_fields["artifactType"]: contract["artifactTypes"]["manifest"],
            manifest_fields["schemaVersion"]: rules["schemaVersion"],
            manifest_fields["invocationIdentity"]: invocation_id,
            manifest_fields["state"]: states["prepared"],
            manifest_fields["requestSha256"]: request_sha,
            manifest_fields["templateJsonSha256"]: template_sha,
            manifest_fields["productionPinSha256"]: pin_sha,
            manifest_fields["referenceImageSha256"]: None,
            manifest_fields["caseSubmissionSha256"]: {},
            manifest_fields["caseWalBindings"]: {},
            manifest_fields["reportSha256"]: None,
            manifest_fields["caseIdentities"]: [
                case[case_fields["caseIdentity"]]
                for case in normalized_request[request_fields["cases"]]
            ],
            manifest_fields["updatedAt"]: timestamp,
        }
        _write_mutable(manifest_path, manifest)
        _write_new_or_same(pin_path, _json_bytes(pin))
        _write_new_or_same(request_path, _json_bytes(normalized_request))
    case_reports: list[dict[str, Any]] = []
    active_case: dict[str, Any] | None = None
    active_prompt: str | None = None
    active_user_input: dict[str, Any] | None = None
    try:
        active_case = normalized_request[request_fields["cases"]][0]
        active_prompt, active_user_input = prompts[0]
        try:
            reference_path, reference_sha = _fetch_reference(
                adapters, template["referenceImage"], output_dir, contract
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            if resumed:
                raise TemplateTestIntegrityError(
                    "frozen reference image cannot be replayed"
                ) from exc
            raise
        if manifest.get(manifest_fields["referenceImageSha256"]) not in {
            None,
            reference_sha,
        }:
            raise ValueError("template reference image digest changed")
        manifest[manifest_fields["referenceImageSha256"]] = reference_sha
        manifest[manifest_fields["state"]] = states["running"]
        manifest[manifest_fields["updatedAt"]] = timestamp
        _write_mutable(manifest_path, manifest)
        if resumed and report_path.exists():
            try:
                report_payload = report_path.read_bytes()
                forward_report = json.loads(report_payload)
            except (OSError, json.JSONDecodeError) as exc:
                raise TemplateTestIntegrityError(
                    "forward report is unreadable"
                ) from exc
            forward_outcome = (
                forward_report.get(report_fields["outcome"])
                if isinstance(forward_report, dict)
                else None
            )
            forward_valid = _completed_report_valid(
                forward_report,
                output_dir=output_dir,
                contract=contract,
                invocation_id=invocation_id,
                template=template,
                template_sha=template_sha,
                template_revision=normalized_request[
                    request_fields["templateRevision"]
                ],
                pin=pin,
                request_sha=request_sha,
                case_ids=manifest[manifest_fields["caseIdentities"]],
                template_path=template_path,
                rules=rules,
                normalized_cases=normalized_request[request_fields["cases"]],
                prompts=prompts,
            )
            forward_valid = forward_valid and not report_path.is_symlink()
            if forward_outcome == "completed":
                forward_valid = forward_valid and _completed_cases_replay_valid(
                    normalized_cases=normalized_request[request_fields["cases"]],
                    prompts=prompts,
                    report_cases=forward_report.get(report_fields["cases"]),
                    template=template,
                    template_sha=template_sha,
                    pin_sha=pin_sha,
                    reference_path=reference_path,
                    reference_sha=reference_sha,
                    output_dir=output_dir,
                    adapters=adapters,
                    rules=rules,
                    timestamp=timestamp,
                    submission_sha_by_case=manifest[
                        manifest_fields["caseSubmissionSha256"]
                    ],
                    wal_bindings_by_case=manifest[
                        manifest_fields["caseWalBindings"]
                    ],
                )
            elif forward_outcome == "failed" and forward_valid:
                completed_prefix = forward_report[report_fields["cases"]][:-1]
                completed_count = len(completed_prefix)
                if completed_count:
                    forward_valid = _completed_cases_replay_valid(
                        normalized_cases=normalized_request[
                            request_fields["cases"]
                        ][:completed_count],
                        prompts=prompts[:completed_count],
                        report_cases=completed_prefix,
                        template=template,
                        template_sha=template_sha,
                        pin_sha=pin_sha,
                        reference_path=reference_path,
                        reference_sha=reference_sha,
                        output_dir=output_dir,
                        adapters=adapters,
                        rules=rules,
                        timestamp=timestamp,
                        submission_sha_by_case=manifest[
                            manifest_fields["caseSubmissionSha256"]
                        ],
                        wal_bindings_by_case=manifest[
                            manifest_fields["caseWalBindings"]
                        ],
                    )
                failed_index = len(forward_report[report_fields["cases"]]) - 1
                forward_valid = forward_valid and _failed_case_lineage_valid(
                    case=normalized_request[request_fields["cases"]][failed_index],
                    resolved_prompt=prompts[failed_index][0],
                    report_error_code=forward_report[report_fields["errorCode"]],
                    template=template,
                    template_sha=template_sha,
                    pin_sha=pin_sha,
                    reference_path=reference_path,
                    reference_sha=reference_sha,
                    output_dir=output_dir,
                    submission_sha_by_case=manifest[
                        manifest_fields["caseSubmissionSha256"]
                    ],
                    wal_bindings_by_case=manifest[
                        manifest_fields["caseWalBindings"]
                    ],
                    rules=rules,
                )
            if not forward_valid:
                raise TemplateTestIntegrityError(
                    "forward report does not match frozen execution"
                )
            manifest[manifest_fields["state"]] = (
                states["completed"]
                if forward_outcome == "completed"
                else states["failed"]
            )
            manifest[manifest_fields["reportSha256"]] = _sha_bytes(
                report_payload
            )
            manifest[manifest_fields["updatedAt"]] = timestamp
            _write_mutable(manifest_path, manifest)
            return TemplateTestResult(
                forward_outcome,
                invocation_id,
                manifest[manifest_fields["state"]],
                output_dir,
                report_path,
                error_code=(
                    None
                    if forward_outcome == "completed"
                    else forward_report[report_fields["errorCode"]]
                ),
                message=(
                    None
                    if forward_outcome == "completed"
                    else "T1 已冻结失败报告；使用新 invocation ID 重试。"
                ),
                resumed=True,
            )
        case_resumed = False
        for case, (resolved_prompt, user_input) in zip(
            normalized_request[request_fields["cases"]], prompts, strict=True
        ):
            active_case = case
            active_prompt = resolved_prompt
            active_user_input = user_input
            case_id = case[case_fields["caseIdentity"]]
            case_dir = output_dir / f"case-{case_id}"

            def bind_submission_sha(
                submission_sha: str, *, current_case_id: str = case_id
            ) -> None:
                bindings = manifest[manifest_fields["caseSubmissionSha256"]]
                existing_sha = bindings.get(current_case_id)
                if existing_sha not in {None, submission_sha}:
                    raise TemplateTestIntegrityError(
                        "submission digest binding changed"
                    )
                bindings[current_case_id] = submission_sha
                manifest[manifest_fields["updatedAt"]] = timestamp
                _write_mutable(manifest_path, manifest)

            def bind_wal(
                binding: dict[str, Any], *, current_case_id: str = case_id
            ) -> None:
                bindings = manifest[manifest_fields["caseWalBindings"]]
                bindings[current_case_id] = binding
                manifest[manifest_fields["updatedAt"]] = timestamp
                _write_mutable(manifest_path, manifest)

            generation, was_resumed = _case_generation(
                case=case,
                resolved_prompt=resolved_prompt,
                template=template,
                template_sha=template_sha,
                pin_sha=pin_sha,
                reference_path=reference_path,
                reference_sha=reference_sha,
                case_dir=case_dir,
                adapters=adapters,
                rules=rules,
                timestamp=timestamp,
                expected_submission_sha=manifest[
                    manifest_fields["caseSubmissionSha256"]
                ].get(case_id),
                record_submission_sha=bind_submission_sha,
                expected_wal_binding=manifest[
                    manifest_fields["caseWalBindings"]
                ].get(case_id),
                record_wal_binding=bind_wal,
            )
            case_resumed = case_resumed or was_resumed
            case_reports.append(
                {
                    case_report_fields["caseIdentity"]: case_id,
                    case_report_fields["mode"]: case[case_fields["mode"]],
                    case_report_fields["userInput"]: user_input,
                    case_report_fields["resolvedPrompt"]: resolved_prompt,
                    **generation,
                    case_report_fields["outcome"]: "completed",
                    case_report_fields["errorCode"]: None,
                    case_report_fields["message"]: None,
                }
            )
        if not _completed_cases_replay_valid(
            normalized_cases=normalized_request[request_fields["cases"]],
            prompts=prompts,
            report_cases=case_reports,
            template=template,
            template_sha=template_sha,
            pin_sha=pin_sha,
            reference_path=reference_path,
            reference_sha=reference_sha,
            output_dir=output_dir,
            adapters=adapters,
            rules=rules,
            timestamp=timestamp,
            submission_sha_by_case=manifest[
                manifest_fields["caseSubmissionSha256"]
            ],
            wal_bindings_by_case=manifest[manifest_fields["caseWalBindings"]],
        ):
            raise TemplateTestIntegrityError(
                "completed T1 cases changed before report finalization"
            )
        if not _frozen_test_inputs_match(
            template_path=template_path,
            template_bytes=template_bytes,
            pin_path=pin_path,
            pin=pin,
            request_path=request_path,
            normalized_request=normalized_request,
            reference_path=reference_path,
            reference_sha=reference_sha,
        ):
            raise TemplateTestIntegrityError(
                "frozen T1 inputs changed during execution"
            )
        report = _build_report(
            rules=rules,
            contract=contract,
            invocation_id=invocation_id,
            template=template,
            template_path=template_path,
            template_sha=template_sha,
            template_revision=normalized_request[
                request_fields["templateRevision"]
            ],
            pin=pin,
            request_sha=request_sha,
            cases=case_reports,
            timestamp=timestamp,
            outcome="completed",
            error_code=None,
        )
        report_payload = _json_bytes(report)
        _write_new_or_same(report_path, report_payload)
        manifest[manifest_fields["state"]] = states["completed"]
        manifest[manifest_fields["reportSha256"]] = _sha_bytes(report_payload)
        manifest[manifest_fields["updatedAt"]] = timestamp
        _write_mutable(manifest_path, manifest)
        return TemplateTestResult(
            "completed",
            invocation_id,
            states["completed"],
            output_dir,
            report_path,
            resumed=resumed or case_resumed,
        )
    except SystemExit:
        raise
    except TemplateTestRoutedGenerationStop as exc:
        route = _generation_failure_route(exc.failure_class, rules)
        manifest[manifest_fields["state"]] = route["state"]
        manifest[manifest_fields["updatedAt"]] = timestamp
        _write_mutable(manifest_path, manifest)
        return TemplateTestResult(
            route["outcome"],
            invocation_id,
            route["state"],
            output_dir,
            error_code=route["errorCode"],
            message="生成供应商要求人工复核或重新规划；禁止自动重提。",
            resumed=resumed,
        )
    except TemplateTestNeedsInput:
        route = _generation_failure_route(
            rules["generationExecutionContract"]["failureClasses"][
                "submissionUnknown"
            ],
            rules,
        )
        manifest[manifest_fields["state"]] = route["state"]
        manifest[manifest_fields["updatedAt"]] = timestamp
        _write_mutable(manifest_path, manifest)
        return TemplateTestResult(
            route["outcome"],
            invocation_id,
            route["state"],
            output_dir,
            error_code=route["errorCode"],
            message="生成提交状态不确定；需先向供应商核对，禁止自动重提。",
            resumed=resumed,
        )
    except TemplateTestIntegrityError:
        return TemplateTestResult(
            "blocked",
            invocation_id,
            states["blocked"],
            output_dir,
            error_code=errors["integrityFailure"],
            message="T1 冻结生成证据完整性校验失败。",
            resumed=resumed,
        )
    except RetryableTemplateTestGeneration:
        route = _generation_failure_route(
            rules["generationExecutionContract"]["failureClasses"]["retryable"],
            rules,
        )
        manifest[manifest_fields["state"]] = route["state"]
        manifest[manifest_fields["updatedAt"]] = timestamp
        _write_mutable(manifest_path, manifest)
        return TemplateTestResult(
            route["outcome"],
            invocation_id,
            route["state"],
            output_dir,
            error_code=route["errorCode"],
            message="生成轮询暂时失败；重跑将复用同一 request ID。",
            resumed=resumed,
        )
    except (OSError, TypeError, ValueError, KeyError, RuntimeError) as exc:
        if "reference_path" in locals() and not _frozen_test_inputs_match(
            template_path=template_path,
            template_bytes=template_bytes,
            pin_path=pin_path,
            pin=pin,
            request_path=request_path,
            normalized_request=normalized_request,
            reference_path=reference_path,
            reference_sha=reference_sha,
        ):
            return TemplateTestResult(
                "blocked",
                invocation_id,
                states["blocked"],
                output_dir,
                error_code=errors["integrityFailure"],
                message="T1 执行期间冻结输入发生变化。",
                resumed=resumed,
            )
        terminal_error = errors["externalFailure"]
        if isinstance(exc, TemplateTestPermanentGenerationFailure):
            terminal_error = _generation_failure_route(
                rules["generationExecutionContract"]["failureClasses"]["permanent"],
                rules,
            )["errorCode"]
        if active_case is not None:
            case_reports.append(
                {
                    case_report_fields["caseIdentity"]: active_case[
                        case_fields["caseIdentity"]
                    ],
                    case_report_fields["mode"]: active_case[
                        case_fields["mode"]
                    ],
                    case_report_fields["userInput"]: active_user_input,
                    case_report_fields["resolvedPrompt"]: active_prompt,
                    case_report_fields["generationRequest"]: (
                        _case_generation_request(
                            active_case,
                            active_prompt,
                            template,
                            template_sha,
                            rules,
                        )
                    ),
                    case_report_fields["outputImagePath"]: None,
                    case_report_fields["outputImageSha256"]: None,
                    case_report_fields["visibleDeviations"]: [],
                    case_report_fields["reviewPass"]: False,
                    case_report_fields["outcome"]: "failed",
                    case_report_fields["errorCode"]: terminal_error,
                    case_report_fields["message"]: type(exc).__name__,
                }
            )
        report = _build_report(
            rules=rules,
            contract=contract,
            invocation_id=invocation_id,
            template=template,
            template_path=template_path,
            template_sha=template_sha,
            template_revision=normalized_request[
                request_fields["templateRevision"]
            ],
            pin=pin,
            request_sha=request_sha,
            cases=case_reports,
            timestamp=timestamp,
            outcome="failed",
            error_code=terminal_error,
        )
        report_payload = _json_bytes(report)
        _write_new_or_same(report_path, report_payload)
        manifest[manifest_fields["state"]] = states["failed"]
        manifest[manifest_fields["reportSha256"]] = _sha_bytes(report_payload)
        manifest[manifest_fields["updatedAt"]] = timestamp
        _write_mutable(manifest_path, manifest)
        return TemplateTestResult(
            "failed",
            invocation_id,
            states["failed"],
            output_dir,
            report_path,
            error_code=terminal_error,
            message=type(exc).__name__,
            resumed=resumed,
        )
