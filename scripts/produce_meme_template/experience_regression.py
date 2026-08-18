from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from typing import Any

from .artifacts import (
    canonical_json_bytes as _canonical_bytes,
    load_json_object as _load_object,
    pretty_json_bytes,
    sha256_bytes as _sha_bytes,
)
from .release_management import runtime_production_pin
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_pretty_bytes = partial(pretty_json_bytes, sort_keys=True)


def _optional_file_sha(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return _sha_bytes(path.read_bytes())
    except OSError:
        return None


def _safe_relative_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return bool(
        not path.is_absolute()
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _ordinary_file(root: Path, relative: Any) -> Path | None:
    if not _safe_relative_path(relative):
        return None
    root = root.resolve()
    path = root / relative
    try:
        if (
            not path.is_file()
            or path.is_symlink()
            or not path.resolve().is_relative_to(root)
        ):
            return None
    except OSError:
        return None
    return path


def _json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise KeyError(pointer)
    current = value
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise KeyError(pointer)
    return current


def _python_symbol_exists(path: Path, locator: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == locator
        for node in ast.walk(tree)
    )


def _test_id_exists(runtime_root: Path, test_id: str) -> bool:
    parts = test_id.split(".")
    if len(parts) < 4 or parts[0] != "tests":
        return False
    module_parts = parts[:-2]
    class_name, method_name = parts[-2:]
    path = _ordinary_file(runtime_root, "/".join(module_parts) + ".py")
    if path is None:
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return any(
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == method_name
                for child in node.body
            )
    return False


def evidence_test_observes_gate(
    runtime_root: Path,
    test_id: str,
    gate_locator: str | None,
) -> bool:
    """Bind a passing unittest to the machine role it explicitly asserts."""
    if gate_locator is None:
        return True
    parts = test_id.split(".")
    if len(parts) < 4 or parts[0] != "tests":
        return False
    path = _ordinary_file(runtime_root, "/".join(parts[:-2]) + ".py")
    if path is None:
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    class_name, method_name = parts[-2:]
    gate_role = gate_locator.rsplit("/", 1)[-1]
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if (
                    isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef)
                    )
                    and child.name == method_name
                ):
                    return gate_role in {
                        literal.value
                        for literal in ast.walk(child)
                        if isinstance(literal, ast.Constant)
                        and isinstance(literal.value, str)
                    }
    return False


class EvidenceStatusRecordingResult(unittest.TextTestResult):
    """Record actual unittest outcomes without treating discovery as execution."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.evidence_status_by_test_id: dict[str, str] = {}
        self.evidence_detail_by_test_id: dict[str, str | None] = {}

    def _record(
        self, test: unittest.TestCase, status: str, detail: str | None
    ) -> None:
        self.evidence_status_by_test_id[test.id()] = status
        self.evidence_detail_by_test_id[test.id()] = detail

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._record(test, "passed", None)

    def addFailure(self, test: unittest.TestCase, err: object) -> None:
        super().addFailure(test, err)
        self._record(test, "failed", err[0].__name__)

    def addError(self, test: unittest.TestCase, err: object) -> None:
        super().addError(test, err)
        self._record(test, "failed", err[0].__name__)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._record(test, "unavailable", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: object) -> None:
        super().addExpectedFailure(test, err)
        self._record(test, "unavailable", "expected failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._record(test, "failed", "unexpected success")


def compile_evidence_execution_results(
    runtime_root: Path,
    test_ids: list[str],
    status_by_test_id: dict[str, str],
    detail_by_test_id: dict[str, str | None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Enrich actual unittest outcomes with machine-bound observed gates."""
    rules = _load_object(runtime_root / "contracts" / "machine-rules.json")
    contract = rules["historicalExperienceContract"]
    matrix = _load_object(runtime_root / contract["matrixRelativePath"])
    evidence_fields = contract["evidenceFields"]
    gate_by_test_id = {
        evidence[evidence_fields["testId"]]: evidence.get(
            evidence_fields["expectedGateLocator"]
        )
        for experience in matrix[contract["matrixFields"]["experiences"]]
        for evidence in experience[contract["experienceFields"]["evidence"]]
    }
    outcomes = contract["evidenceOutcomes"]
    fields = contract["evidenceResultFields"]
    details = detail_by_test_id or {}
    results: dict[str, dict[str, Any]] = {}
    for test_id in test_ids:
        status = status_by_test_id.get(test_id, "unavailable")
        gate_locator = gate_by_test_id.get(test_id)
        detail = details.get(test_id)
        if status == "passed" and not evidence_test_observes_gate(
            runtime_root, test_id, gate_locator
        ):
            status = "unavailable"
            detail = "test does not observe expected machine gate"
        gate_value = None
        if status == "passed" and gate_locator is not None:
            gate_value = _json_pointer(rules, gate_locator)
        results[test_id] = {
            fields["outcome"]: outcomes[status],
            fields["detail"]: detail if status != "passed" else None,
            fields["observedGateLocator"]: (
                gate_locator if status == "passed" else None
            ),
            fields["observedGateValue"]: gate_value,
        }
    return results


def _pin_value(
    pin: dict[str, Any], release_contract: dict[str, Any], role: str
) -> Any:
    if role == "skillVersion":
        pin_fields = release_contract["productionPinFields"]
        skill_fields = release_contract["productionPinSkillFields"]
        skill = pin.get(pin_fields["skill"])
        if isinstance(skill, dict):
            return skill.get(skill_fields["version"])
    field = release_contract["productionPinFields"].get(role, role)
    return pin.get(field, pin.get(role))


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


class ExperienceRegressionAdapters:
    """Replaceable execution boundary for runtime identity and test evidence."""

    def runtime_pin(self, runtime_root: Path) -> dict[str, Any]:
        return runtime_production_pin(runtime_root)

    def execute_evidence(
        self, runtime_root: Path, test_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        rules = _load_object(runtime_root / "contracts" / "machine-rules.json")
        contract = rules["historicalExperienceContract"]
        unavailable = contract["evidenceOutcomes"]["unavailable"]
        result_fields = contract["evidenceResultFields"]
        runner = _ordinary_file(
            runtime_root, contract["evidenceRunnerRelativePath"]
        )
        if runner is None:
            return {
                test_id: {
                    result_fields["outcome"]: unavailable,
                    result_fields["detail"]: "runner missing",
                    result_fields["observedGateLocator"]: None,
                    result_fields["observedGateValue"]: None,
                }
                for test_id in test_ids
            }
        command = [sys.executable, str(runner)]
        for test_id in test_ids:
            command.extend(("--test-id", test_id))
        try:
            completed = subprocess.run(
                command,
                cwd=runtime_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=rules["releaseManagementContract"][
                    "validationTimeoutSeconds"
                ],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict):
                raise ValueError("evidence runner result must be an object")
            return payload
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            return {
                test_id: {
                    result_fields["outcome"]: unavailable,
                    result_fields["detail"]: type(exc).__name__,
                    result_fields["observedGateLocator"]: None,
                    result_fields["observedGateValue"]: None,
                }
                for test_id in test_ids
            }

    def formal_contract_valid(
        self, runtime_root: Path, record: dict[str, Any]
    ) -> bool | None:
        program = (
            "import json,sys;"
            "from scripts.produce_meme_template.workflow import "
            "formal_template_contract_valid;"
            "rules=json.load(open('contracts/machine-rules.json'));"
            "record=json.load(sys.stdin);"
            "print(json.dumps(formal_template_contract_valid(record,rules)))"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", program],
                cwd=runtime_root,
                input=json.dumps(record, ensure_ascii=False),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if completed.returncode != 0:
                return None
            value = json.loads(completed.stdout)
            return value if isinstance(value, bool) else None
        except (
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return None


def _matrix_contract_valid(contract: Any) -> bool:
    mapping_roles = {
        "requiredCorpusRoles": {
            "latestHeartInput",
            "latestHeartExpected",
            "latestWeddingInput",
            "latestWeddingExpected",
            "ordinaryPerson",
            "knownCharacterIp",
            "animal",
            "visibleText",
            "complexComposition",
        },
        "corpusContentKinds": {"jsonPointer", "ppmImage"},
        "corpusBindingFields": {
            "path",
            "selectorPointer",
            "selectorValue",
            "contentKind",
        },
        "migrationStatuses": {
            "confirmed",
            "conditional",
            "rewritten",
            "retired",
        },
        "evidencePolarities": {"goodCase", "badCase", "humanReview"},
        "evidenceExpectationFields": {
            "polarityRole",
            "expectedGateLocator",
        },
        "implementationKinds": {
            "machineRule",
            "pythonSymbol",
            "documentedBoundary",
        },
        "evidenceOutcomes": {"passed", "failed", "unavailable"},
        "evidenceResultFields": {
            "outcome",
            "detail",
            "observedGateLocator",
            "observedGateValue",
        },
        "outcomes": {"passed", "failed"},
        "failureCategories": {
            "ruleMissing",
            "fixtureMissing",
            "externalAdapterUnavailable",
            "formalContractIncompatible",
            "versionDrift",
            "evidenceFailure",
        },
        "matrixFields": {"schemaVersion", "experiences", "corpus"},
        "experienceFields": {
            "experienceId",
            "topic",
            "migrationStatus",
            "legacyDisposition",
            "authority",
            "implementation",
            "evidence",
        },
        "authorityFields": {"path", "anchor"},
        "implementationFields": {"kind", "path", "locator"},
        "evidenceFields": {
            "testId",
            "polarity",
            "fixturePaths",
            "expectedGateLocator",
        },
        "corpusFields": {"path", "selector", "sha256"},
        "reportFields": {
            "artifactType",
            "schemaVersion",
            "pass",
            "outcome",
            "runtimePin",
            "runtimePinSha256",
            "releaseManifestSha256",
            "machineRulesSha256",
            "matrixSha256",
            "experiences",
            "corpus",
            "failureCategories",
            "summary",
        },
        "reportExperienceFields": {
            "experienceId",
            "authority",
            "implementation",
            "evidence",
            "migrationStatus",
            "legacyDisposition",
            "pass",
            "failureCategories",
        },
        "reportEvidenceFields": {
            "testId",
            "polarity",
            "outcome",
            "detail",
            "fixturePaths",
            "expectedGateLocator",
            "expectedGateValue",
            "observedGateLocator",
            "observedGateValue",
        },
        "reportCorpusFields": {
            "path",
            "selector",
            "sha256",
            "pass",
            "failureCategory",
        },
        "summaryFields": {"total", "passed", "failed"},
    }
    if not isinstance(contract, dict):
        return False
    for role, expected_roles in mapping_roles.items():
        mapping = contract.get(role)
        if not (
            isinstance(mapping, dict)
            and set(mapping) == expected_roles
            and len(mapping) == len(set(mapping.values()))
            and all(isinstance(item, str) and item for item in mapping.values())
        ):
            return False
    corpus_roles = contract["requiredCorpusRoles"]
    binding_fields = contract["corpusBindingFields"]
    bindings = contract.get("requiredCorpusBindings")
    if not (
        isinstance(bindings, dict)
        and set(bindings) == set(corpus_roles)
        and all(
            isinstance(binding, dict)
            and set(binding) == set(binding_fields.values())
            and _safe_relative_path(binding.get(binding_fields["path"]))
            and (
                binding.get(binding_fields["selectorPointer"]) is None
                or isinstance(
                    binding.get(binding_fields["selectorPointer"]), str
                )
                and binding[binding_fields["selectorPointer"]].startswith("/")
            )
            and isinstance(binding.get(binding_fields["selectorValue"]), str)
            and binding[binding_fields["selectorValue"]]
            and binding.get(binding_fields["contentKind"])
            in contract["corpusContentKinds"].values()
            for binding in bindings.values()
        )
    ):
        return False
    retired = contract.get("retiredRepositoryPrefixes")
    if not (
        isinstance(retired, list)
        and retired
        and len(retired) == len(set(retired))
        and all(_safe_relative_path(value) for value in retired)
    ):
        return False
    expectations = contract.get("requiredEvidenceContracts")
    experience_digests = contract.get("requiredExperienceSha256ById")
    expectation_fields = contract["evidenceExpectationFields"]
    if not (
        isinstance(expectations, dict)
        and set(expectations) == set(contract["experienceIds"])
        and all(
            isinstance(items, list)
            and items
            and all(
                isinstance(item, dict)
                and set(item) == set(expectation_fields.values())
                and item.get(expectation_fields["polarityRole"])
                in contract["evidencePolarities"]
                and (
                    item.get(expectation_fields["expectedGateLocator"])
                    is None
                    or isinstance(
                        item.get(
                            expectation_fields["expectedGateLocator"]
                        ),
                        str,
                    )
                    and item[
                        expectation_fields["expectedGateLocator"]
                    ].startswith("/")
                )
                for item in items
            )
            for items in expectations.values()
        )
    ):
        return False
    if not (
        isinstance(experience_digests, dict)
        and set(experience_digests) == set(contract["experienceIds"])
        and all(
            isinstance(value, str) and SHA256_PATTERN.fullmatch(value)
            for value in experience_digests.values()
        )
    ):
        return False
    ids = contract.get("experienceIds")
    return bool(
        isinstance(contract.get("artifactType"), str)
        and contract["artifactType"]
        and _safe_relative_path(contract.get("matrixRelativePath"))
        and _safe_relative_path(contract.get("evidenceRunnerRelativePath"))
        and isinstance(ids, list)
        and len(ids) == 39
        and len(ids) == len(set(ids))
        and ids == [f"E{index:02d}" for index in range(1, 40)]
    )


def _matrix_shape_valid(matrix: Any, contract: dict[str, Any]) -> bool:
    fields = contract["matrixFields"]
    return bool(
        isinstance(matrix, dict)
        and set(matrix) == set(fields.values())
        and isinstance(matrix.get(fields["schemaVersion"]), str)
        and isinstance(matrix.get(fields["experiences"]), list)
        and isinstance(matrix.get(fields["corpus"]), dict)
    )


def _validate_authority(
    runtime_root: Path,
    value: Any,
    contract: dict[str, Any],
) -> bool:
    fields = contract["authorityFields"]
    if not (
        isinstance(value, dict)
        and set(value) == set(fields.values())
        and isinstance(value.get(fields["anchor"]), str)
        and value[fields["anchor"]]
    ):
        return False
    path = _ordinary_file(runtime_root, value.get(fields["path"]))
    if path is None:
        return False
    try:
        return value[fields["anchor"]] in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _validate_implementation(
    runtime_root: Path,
    value: Any,
    contract: dict[str, Any],
) -> bool:
    fields = contract["implementationFields"]
    kinds = contract["implementationKinds"]
    if not (
        isinstance(value, dict)
        and set(value) == set(fields.values())
        and value.get(fields["kind"]) in kinds.values()
        and isinstance(value.get(fields["locator"]), str)
        and value[fields["locator"]]
    ):
        return False
    path = _ordinary_file(runtime_root, value.get(fields["path"]))
    if path is None:
        return False
    kind = value[fields["kind"]]
    locator = value[fields["locator"]]
    if kind == kinds["pythonSymbol"]:
        return _python_symbol_exists(path, locator)
    if kind == kinds["documentedBoundary"]:
        try:
            return locator in path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
    try:
        _json_pointer(_load_object(path), locator)
        return True
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def _validate_evidence_shape(
    value: Any, contract: dict[str, Any], rules: dict[str, Any]
) -> bool:
    fields = contract["evidenceFields"]
    if not isinstance(value, dict):
        return False
    fixture_paths = value.get(fields["fixturePaths"])
    polarity = value.get(fields["polarity"])
    expected_gate = value.get(fields["expectedGateLocator"])
    required_keys = {
        fields["testId"],
        fields["polarity"],
        fields["fixturePaths"],
    }
    if polarity != contract["evidencePolarities"]["goodCase"]:
        required_keys.add(fields["expectedGateLocator"])
    if set(value) != required_keys:
        return False
    gate_valid = expected_gate is None
    if polarity != contract["evidencePolarities"]["goodCase"]:
        try:
            gate_valid = bool(
                isinstance(expected_gate, str)
                and expected_gate.startswith("/")
                and _json_pointer(rules, expected_gate) is not None
            )
        except KeyError:
            gate_valid = False
    return bool(
        isinstance(value.get(fields["testId"]), str)
        and value[fields["testId"]]
        and polarity in contract["evidencePolarities"].values()
        and isinstance(fixture_paths, list)
        and fixture_paths
        and all(_safe_relative_path(item) for item in fixture_paths)
        and len(fixture_paths) == len(set(fixture_paths))
        and gate_valid
    )


def _runtime_version_failures(
    runtime_root: Path,
    pin: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    del rules
    try:
        expected = runtime_production_pin(runtime_root)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return ["runtimePin"]
    return [] if pin == expected else ["runtimePin"]


def _corpus_report(
    runtime_root: Path,
    matrix: dict[str, Any],
    rules: dict[str, Any],
    contract: dict[str, Any],
    adapters: ExperienceRegressionAdapters,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    matrix_fields = contract["matrixFields"]
    corpus_fields = contract["corpusFields"]
    report_fields = contract["reportCorpusFields"]
    failures = contract["failureCategories"]
    corpus_roles = contract["requiredCorpusRoles"]
    expected_roles = set(corpus_roles.values())
    binding_fields = contract["corpusBindingFields"]
    content_kinds = contract["corpusContentKinds"]
    bindings = contract["requiredCorpusBindings"]
    corpus = matrix[matrix_fields["corpus"]]
    report: dict[str, dict[str, Any]] = {}
    categories: list[str] = []
    if set(corpus) != expected_roles:
        _append_unique(categories, failures["fixtureMissing"])
    for role in sorted(expected_roles):
        role_key = next(key for key, value in corpus_roles.items() if value == role)
        binding = bindings[role_key]
        entry = corpus.get(role)
        category: str | None = None
        path: Path | None = None
        if not (
            isinstance(entry, dict)
            and set(entry) == set(corpus_fields.values())
            and _safe_relative_path(entry.get(corpus_fields["path"]))
            and (
                entry.get(corpus_fields["selector"]) is None
                or isinstance(entry.get(corpus_fields["selector"]), str)
                and entry[corpus_fields["selector"]]
            )
            and isinstance(entry.get(corpus_fields["sha256"]), str)
            and SHA256_PATTERN.fullmatch(entry[corpus_fields["sha256"]])
        ):
            category = failures["fixtureMissing"]
        else:
            declared_path = entry[corpus_fields["path"]]
            declared_selector = entry[corpus_fields["selector"]]
            expected_path = binding[binding_fields["path"]]
            expected_pointer = binding[binding_fields["selectorPointer"]]
            if declared_path != expected_path or declared_selector != expected_pointer:
                category = failures["fixtureMissing"]
            path = _ordinary_file(runtime_root, declared_path)
            if path is None:
                category = failures["fixtureMissing"]
            elif _sha_bytes(path.read_bytes()) != entry[corpus_fields["sha256"]]:
                category = failures["versionDrift"]
            elif binding[binding_fields["contentKind"]] == content_kinds["jsonPointer"]:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        not isinstance(payload, dict)
                        or _json_pointer(payload, expected_pointer)
                        != binding[binding_fields["selectorValue"]]
                    ):
                        category = failures["fixtureMissing"]
                except (
                    OSError,
                    KeyError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                ):
                    category = failures["fixtureMissing"]
            elif not path.read_bytes().startswith(
                binding[binding_fields["selectorValue"]].encode("ascii") + b"\n"
            ):
                category = failures["fixtureMissing"]
        if path is not None and role in {
            corpus_roles["latestHeartExpected"],
            corpus_roles["latestWeddingExpected"],
        }:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                formal_result = adapters.formal_contract_valid(
                    runtime_root, record
                )
                if type(formal_result) is not bool:
                    category = failures["externalAdapterUnavailable"]
                elif not formal_result:
                    category = failures["formalContractIncompatible"]
            except (
                OSError,
                TypeError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ):
                category = failures["externalAdapterUnavailable"]
        if category is not None:
            _append_unique(categories, category)
        source = entry if isinstance(entry, dict) else {}
        report[role] = {
            report_fields["path"]: source.get(corpus_fields["path"]),
            report_fields["selector"]: source.get(corpus_fields["selector"]),
            report_fields["sha256"]: source.get(corpus_fields["sha256"]),
            report_fields["pass"]: category is None,
            report_fields["failureCategory"]: category,
        }
    return report, categories


def _write_create_once_report(
    output_path: Path | None,
    runtime_root: Path,
    report: dict[str, Any],
) -> None:
    if output_path is None:
        return
    if ".." in output_path.parts:
        raise ValueError("regression report path cannot contain parent traversal")
    target = output_path.absolute()
    current = Path(target.anchor)
    for part in target.parts[1:]:
        candidate = current / part
        if candidate.is_symlink():
            if current == Path(target.anchor):
                current = candidate.resolve()
                continue
            raise ValueError("regression report path cannot contain a symlink")
        current = candidate
    if current.is_relative_to(runtime_root):
        raise ValueError("regression report must be outside the runtime")
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = target.parent.resolve()
    resolved_target = resolved_parent / target.name
    if resolved_target.is_relative_to(runtime_root):
        raise ValueError("regression report must be outside the runtime")
    payload = _pretty_bytes(report)
    if target.exists():
        if (
            not target.is_file()
            or target.is_symlink()
            or target.read_bytes() != payload
        ):
            raise ValueError("immutable regression report conflict")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, target, follow_symlinks=False)
        except FileExistsError:
            if (
                not target.is_file()
                or target.is_symlink()
                or target.read_bytes() != payload
            ):
                raise ValueError("immutable regression report conflict")
    finally:
        temporary_path.unlink(missing_ok=True)


def _early_failure_report(
    runtime_root: Path,
    output_path: Path | None,
    rules: dict[str, Any],
    contract: dict[str, Any],
    adapters: ExperienceRegressionAdapters,
    category: str,
) -> dict[str, Any]:
    report_fields = contract["reportFields"]
    summary_fields = contract["summaryFields"]
    try:
        claimed_pin = adapters.runtime_pin(runtime_root)
        if not isinstance(claimed_pin, dict):
            claimed_pin = {}
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        claimed_pin = {}
    try:
        runtime_pin = runtime_production_pin(runtime_root)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        runtime_pin = {}
    categories = [category]
    if claimed_pin != runtime_pin or _runtime_version_failures(
        runtime_root, runtime_pin, rules
    ):
        categories.append(contract["failureCategories"]["versionDrift"])
    report = {
        report_fields["artifactType"]: contract["artifactType"],
        report_fields["schemaVersion"]: rules.get("schemaVersion"),
        report_fields["pass"]: False,
        report_fields["outcome"]: contract["outcomes"]["failed"],
        report_fields["runtimePin"]: runtime_pin,
        report_fields["runtimePinSha256"]: _sha_bytes(
            _canonical_bytes(runtime_pin)
        ),
        report_fields["releaseManifestSha256"]: _optional_file_sha(
            runtime_root / "skill-manifest.json"
        ),
        report_fields["machineRulesSha256"]: _optional_file_sha(
            runtime_root / "contracts" / "machine-rules.json"
        ),
        report_fields["matrixSha256"]: None,
        report_fields["experiences"]: [],
        report_fields["corpus"]: {},
        report_fields["failureCategories"]: categories,
        report_fields["summary"]: {
            summary_fields["total"]: len(contract["experienceIds"]),
            summary_fields["passed"]: 0,
            summary_fields["failed"]: len(contract["experienceIds"]),
        },
    }
    _write_create_once_report(output_path, runtime_root, report)
    return report


def run_experience_regression(
    runtime_root: Path,
    output_path: Path | None,
    *,
    adapters: ExperienceRegressionAdapters | None = None,
) -> dict[str, Any]:
    """Compile E01–E39 authority, behavior, fixture and execution evidence."""

    runtime_root = runtime_root.resolve()
    adapters = adapters or ExperienceRegressionAdapters()
    rules = _load_object(runtime_root / "contracts" / "machine-rules.json")
    contract = rules.get("historicalExperienceContract")
    if not _matrix_contract_valid(contract):
        raise ValueError("invalid historical experience contract")
    assert isinstance(contract, dict)
    report_fields = contract["reportFields"]
    experience_fields = contract["experienceFields"]
    report_experience_fields = contract["reportExperienceFields"]
    evidence_fields = contract["evidenceFields"]
    report_evidence_fields = contract["reportEvidenceFields"]
    summary_fields = contract["summaryFields"]
    failures = contract["failureCategories"]
    evidence_outcomes = contract["evidenceOutcomes"]
    evidence_result_fields = contract["evidenceResultFields"]
    matrix_path = _ordinary_file(runtime_root, contract["matrixRelativePath"])
    if matrix_path is None:
        return _early_failure_report(
            runtime_root,
            output_path,
            rules,
            contract,
            adapters,
            failures["fixtureMissing"],
        )
    try:
        matrix = _load_object(matrix_path)
    except (OSError, ValueError, json.JSONDecodeError):
        matrix = None
    if not _matrix_shape_valid(matrix, contract):
        return _early_failure_report(
            runtime_root,
            output_path,
            rules,
            contract,
            adapters,
            failures["fixtureMissing"],
        )
    assert isinstance(matrix, dict)

    global_categories: list[str] = []
    try:
        claimed_pin = adapters.runtime_pin(runtime_root)
        if not isinstance(claimed_pin, dict):
            raise ValueError("runtime pin must be an object")
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        claimed_pin = {}
    try:
        runtime_pin = runtime_production_pin(runtime_root)
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        runtime_pin = {}
    if claimed_pin != runtime_pin or _runtime_version_failures(
        runtime_root, runtime_pin, rules
    ):
        _append_unique(global_categories, failures["versionDrift"])

    matrix_fields = contract["matrixFields"]
    raw_experiences = matrix[matrix_fields["experiences"]]
    expected_ids = contract["experienceIds"]
    actual_ids = [
        item.get(experience_fields["experienceId"])
        if isinstance(item, dict)
        else None
        for item in raw_experiences
    ]
    if actual_ids != expected_ids:
        _append_unique(global_categories, failures["ruleMissing"])
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in raw_experiences:
        if not isinstance(item, dict):
            continue
        item_id = item.get(experience_fields["experienceId"])
        if not isinstance(item_id, str) or item_id not in expected_ids:
            continue
        if item_id in by_id:
            duplicate_ids.add(item_id)
        else:
            by_id[item_id] = item
    experiences = [
        by_id.get(item_id, {}) if item_id not in duplicate_ids else {}
        for item_id in expected_ids
    ]

    test_ids: list[str] = []
    for item in experiences:
        if not isinstance(item, dict):
            continue
        evidence = item.get(experience_fields["evidence"])
        if isinstance(evidence, list):
            for value in evidence:
                if _validate_evidence_shape(value, contract, rules):
                    test_id = value[evidence_fields["testId"]]
                    if test_id not in test_ids:
                        test_ids.append(test_id)
    try:
        execution_results = adapters.execute_evidence(runtime_root, test_ids)
        if not isinstance(execution_results, dict):
            raise ValueError("evidence results must be an object")
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        execution_results = {
            test_id: {
                evidence_result_fields["outcome"]: evidence_outcomes[
                    "unavailable"
                ],
                evidence_result_fields["detail"]: "adapter failure",
                evidence_result_fields["observedGateLocator"]: None,
                evidence_result_fields["observedGateValue"]: None,
            }
            for test_id in test_ids
        }

    report_experiences: list[dict[str, Any]] = []
    for item in experiences:
        item_categories: list[str] = []
        if not isinstance(item, dict) or set(item) != set(experience_fields.values()):
            _append_unique(item_categories, failures["ruleMissing"])
            source: dict[str, Any] = {}
        else:
            source = item
        experience_id = source.get(experience_fields["experienceId"])
        authority = source.get(experience_fields["authority"])
        implementation = source.get(experience_fields["implementation"])
        evidence_values = source.get(experience_fields["evidence"])
        migration_status = source.get(experience_fields["migrationStatus"])
        legacy_disposition = source.get(experience_fields["legacyDisposition"])
        topic = source.get(experience_fields["topic"])
        if not isinstance(topic, str) or not topic.strip():
            _append_unique(item_categories, failures["ruleMissing"])
        if not _validate_authority(runtime_root, authority, contract):
            _append_unique(item_categories, failures["ruleMissing"])
        if not _validate_implementation(runtime_root, implementation, contract):
            _append_unique(item_categories, failures["ruleMissing"])
        if not (
            isinstance(migration_status, str)
            and migration_status in contract["migrationStatuses"].values()
        ):
            _append_unique(item_categories, failures["ruleMissing"])
        if (
            isinstance(migration_status, str)
            and migration_status in {
                contract["migrationStatuses"]["conditional"],
                contract["migrationStatuses"]["rewritten"],
                contract["migrationStatuses"]["retired"],
            }
            and not (
                isinstance(legacy_disposition, str)
                and legacy_disposition.strip()
            )
        ):
            _append_unique(item_categories, failures["ruleMissing"])
        report_evidence: list[dict[str, Any]] = []
        if not isinstance(evidence_values, list) or not evidence_values:
            _append_unique(item_categories, failures["fixtureMissing"])
            evidence_values = []
        expectation_fields = contract["evidenceExpectationFields"]
        expected_evidence = contract["requiredEvidenceContracts"].get(
            experience_id, []
        )
        actual_evidence_contract = [
            {
                expectation_fields["polarityRole"]: next(
                    (
                        role
                        for role, value in contract[
                            "evidencePolarities"
                        ].items()
                        if isinstance(evidence, dict)
                        and evidence.get(evidence_fields["polarity"]) == value
                    ),
                    None,
                ),
                expectation_fields["expectedGateLocator"]: (
                    evidence.get(evidence_fields["expectedGateLocator"])
                    if isinstance(evidence, dict)
                    else None
                ),
            }
            for evidence in evidence_values
        ]
        if actual_evidence_contract != expected_evidence:
            _append_unique(item_categories, failures["ruleMissing"])
        if (
            isinstance(experience_id, str)
            and _sha_bytes(_canonical_bytes(source))
            != contract["requiredExperienceSha256ById"].get(
                experience_id
            )
        ):
            _append_unique(item_categories, failures["ruleMissing"])
        for evidence in evidence_values:
            if not _validate_evidence_shape(evidence, contract, rules):
                _append_unique(item_categories, failures["fixtureMissing"])
                continue
            test_id = evidence[evidence_fields["testId"]]
            fixture_paths = evidence[evidence_fields["fixturePaths"]]
            expected_gate_locator = evidence.get(
                evidence_fields["expectedGateLocator"]
            )
            if (
                not _test_id_exists(runtime_root, test_id)
                or not evidence_test_observes_gate(
                    runtime_root, test_id, expected_gate_locator
                )
                or any(
                _ordinary_file(runtime_root, path) is None
                for path in fixture_paths
                )
            ):
                _append_unique(item_categories, failures["fixtureMissing"])
            execution = execution_results.get(test_id)
            if not (
                isinstance(execution, dict)
                and set(execution) == set(evidence_result_fields.values())
                and execution.get(evidence_result_fields["outcome"])
                in evidence_outcomes.values()
                and (
                    execution.get(evidence_result_fields["detail"]) is None
                    or isinstance(
                        execution.get(evidence_result_fields["detail"]), str
                    )
                )
                and execution.get(
                    evidence_result_fields["observedGateLocator"]
                )
                == expected_gate_locator
                and execution.get(
                    evidence_result_fields["observedGateValue"]
                )
                == (
                    _json_pointer(rules, expected_gate_locator)
                    if isinstance(expected_gate_locator, str)
                    else None
                )
            ):
                execution = {
                    evidence_result_fields["outcome"]: evidence_outcomes[
                        "unavailable"
                    ],
                    evidence_result_fields["detail"]: "missing result",
                    evidence_result_fields["observedGateLocator"]: None,
                    evidence_result_fields["observedGateValue"]: None,
                }
            execution_outcome = execution[evidence_result_fields["outcome"]]
            if execution_outcome == evidence_outcomes["unavailable"]:
                _append_unique(
                    item_categories, failures["externalAdapterUnavailable"]
                )
            elif execution_outcome != evidence_outcomes["passed"]:
                _append_unique(item_categories, failures["evidenceFailure"])
            expected_gate_value = (
                _json_pointer(rules, expected_gate_locator)
                if isinstance(expected_gate_locator, str)
                else None
            )
            report_evidence.append(
                {
                    report_evidence_fields["testId"]: test_id,
                    report_evidence_fields["polarity"]: evidence[
                        evidence_fields["polarity"]
                    ],
                    report_evidence_fields["outcome"]: execution_outcome,
                    report_evidence_fields["detail"]: execution.get(
                        evidence_result_fields["detail"]
                    ),
                    report_evidence_fields["fixturePaths"]: fixture_paths,
                    report_evidence_fields[
                        "expectedGateLocator"
                    ]: expected_gate_locator,
                    report_evidence_fields[
                        "expectedGateValue"
                    ]: expected_gate_value,
                    report_evidence_fields[
                        "observedGateLocator"
                    ]: execution.get(
                        evidence_result_fields["observedGateLocator"]
                    ),
                    report_evidence_fields[
                        "observedGateValue"
                    ]: execution.get(
                        evidence_result_fields["observedGateValue"]
                    ),
                }
            )
        for category in item_categories:
            _append_unique(global_categories, category)
        report_experiences.append(
            {
                report_experience_fields["experienceId"]: experience_id,
                report_experience_fields["authority"]: authority,
                report_experience_fields["implementation"]: implementation,
                report_experience_fields["evidence"]: report_evidence,
                report_experience_fields["migrationStatus"]: migration_status,
                report_experience_fields["legacyDisposition"]: legacy_disposition,
                report_experience_fields["pass"]: not item_categories,
                report_experience_fields["failureCategories"]: item_categories,
            }
        )

    corpus, corpus_categories = _corpus_report(
        runtime_root, matrix, rules, contract, adapters
    )
    for category in corpus_categories:
        _append_unique(global_categories, category)
    if _runtime_version_failures(runtime_root, runtime_pin, rules):
        _append_unique(global_categories, failures["versionDrift"])
    passed_count = sum(
        bool(item[report_experience_fields["pass"]])
        for item in report_experiences
    )
    report_pass = not global_categories and passed_count == len(expected_ids)
    release_manifest_sha = _optional_file_sha(
        runtime_root / "skill-manifest.json"
    )
    machine_rules_sha = _optional_file_sha(
        runtime_root / "contracts" / "machine-rules.json"
    )
    report = {
        report_fields["artifactType"]: contract["artifactType"],
        report_fields["schemaVersion"]: matrix[matrix_fields["schemaVersion"]],
        report_fields["pass"]: report_pass,
        report_fields["outcome"]: contract["outcomes"][
            "passed" if report_pass else "failed"
        ],
        report_fields["runtimePin"]: runtime_pin,
        report_fields["runtimePinSha256"]: _sha_bytes(
            _canonical_bytes(runtime_pin)
        ),
        report_fields["releaseManifestSha256"]: release_manifest_sha,
        report_fields["machineRulesSha256"]: machine_rules_sha,
        report_fields["matrixSha256"]: _sha_bytes(matrix_path.read_bytes()),
        report_fields["experiences"]: report_experiences,
        report_fields["corpus"]: corpus,
        report_fields["failureCategories"]: global_categories,
        report_fields["summary"]: {
            summary_fields["total"]: len(expected_ids),
            summary_fields["passed"]: passed_count,
            summary_fields["failed"]: len(expected_ids) - passed_count,
        },
    }
    _write_create_once_report(output_path, runtime_root, report)
    return report
