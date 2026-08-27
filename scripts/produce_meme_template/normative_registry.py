from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _safe_relative_path(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and not value.startswith(("/", "~"))
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in Path(value).parts)
    )


def _ordinary_file(root: Path, relative: Any) -> Path | None:
    if not _safe_relative_path(relative):
        return None
    path = root.joinpath(*Path(relative).parts)
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file() or path.is_symlink():
        return None
    return path


def _local_markdown_links(markdown: str) -> set[str]:
    links = set()
    for value in re.findall(r"\[[^]]+\]\(([^)]+)\)", markdown):
        target = value.split("#", 1)[0]
        if target.endswith(".md") and not re.match(r"^[a-z]+://", target):
            links.add(target)
    return links


def discover_authority_paths(
    root: Path, contract: dict[str, Any]
) -> list[str]:
    roots = contract["requiredAuthorityPaths"]
    skill_path = contract["skillEntrypoint"]
    retired = set(contract["retiredAuthorityPaths"])
    skill = _ordinary_file(root, skill_path)
    if skill is None:
        return []
    discovered = set(roots)
    discovered.update(_local_markdown_links(skill.read_text(encoding="utf-8")))
    adr_root = root / contract["activeAdrDirectory"]
    if adr_root.is_dir() and not adr_root.is_symlink():
        discovered.update(
            str(path.relative_to(root))
            for path in adr_root.glob("*.md")
            if path.is_file() and not path.is_symlink()
        )
    return sorted(discovered - retired)


def _normalize_unit(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def scan_authority_units(path: Path) -> list[str]:
    """Index every non-heading prose unit; wording cannot evade this scanner."""

    units: list[str] = []
    paragraph: list[str] = []
    in_code = False

    def flush() -> None:
        if paragraph:
            normalized = _normalize_unit(" ".join(paragraph))
            if normalized:
                units.append(normalized)
            paragraph.clear()

    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code or stripped.startswith("<!--"):
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("#") or stripped == "---":
            flush()
            continue
        if re.fullmatch(r"\|?(?:\s*:?-+:?\s*\|)+", stripped):
            flush()
            continue
        if (
            re.match(r"^(?:[-*+] |\d+\. )", stripped)
            or stripped.startswith("|")
            or stripped.startswith("_Avoid_:")
        ):
            flush()
            paragraph.append(stripped)
            flush()
            continue
        paragraph.append(stripped)
    flush()
    return units


def _unit_rows(
    relative: str,
    units: list[str],
    *,
    assignments: dict[str, str],
    contract: dict[str, Any],
) -> list[dict[str, str]]:
    alias = re.sub(r"[^a-z0-9]+", "-", relative.lower()).strip("-")[-32:]
    unit_fields = contract["unitFields"]
    seen: dict[str, int] = {}
    rows = []
    for unit in units:
        digest = _sha_bytes(unit.encode("utf-8"))
        occurrence = seen.get(digest, 0) + 1
        seen[digest] = occurrence
        rule_id = f"NR-{alias}-{digest[:12]}-{occurrence}"
        family_id = assignments.get(rule_id)
        if not isinstance(family_id, str) or not family_id:
            raise ValueError(
                f"authority unit needs an explicit reviewed family: {relative}#{rule_id}"
            )
        rows.append(
            {
                unit_fields["identity"]: rule_id,
                unit_fields["sha256"]: digest,
                unit_fields["family"]: family_id,
            }
        )
    return rows


def _assignment_digest(
    sources: list[dict[str, Any]],
    contract: dict[str, Any],
) -> str:
    source_fields = contract["sourceFields"]
    unit_fields = contract["unitFields"]
    assignments = [
        [
            source[source_fields["path"]],
            unit[unit_fields["identity"]],
            unit[unit_fields["family"]],
        ]
        for source in sources
        for unit in source[source_fields["units"]]
    ]
    return _sha_bytes(_canonical_bytes(assignments))


def refresh_registry(
    root: Path, registry: dict[str, Any], rules: dict[str, Any]
) -> dict[str, Any]:
    contract = rules["normativeRuleRegistryContract"]
    fields = contract["registryFields"]
    source_fields = contract["sourceFields"]
    current_sources = registry.get(fields["sources"], [])
    family_by_path = {
        item[source_fields["path"]]: item[source_fields["family"]]
        for item in current_sources
        if isinstance(item, dict)
        and isinstance(item.get(source_fields["path"]), str)
        and isinstance(item.get(source_fields["family"]), str)
    }
    assignments_by_path = {
        item[source_fields["path"]]: {
            unit[contract["unitFields"]["identity"]]: unit[
                contract["unitFields"]["family"]
            ]
            for unit in item.get(source_fields["units"], [])
            if isinstance(unit, dict)
            and isinstance(
                unit.get(contract["unitFields"]["identity"]), str
            )
            and isinstance(unit.get(contract["unitFields"]["family"]), str)
        }
        for item in current_sources
        if isinstance(item, dict)
        and isinstance(item.get(source_fields["path"]), str)
    }
    discovered = discover_authority_paths(root, contract)
    missing_assignments = sorted(set(discovered) - set(family_by_path))
    if missing_assignments:
        raise ValueError(
            "new authority paths need an enforcement family: "
            + ", ".join(missing_assignments)
        )
    rendered = {
        fields["artifactType"]: contract["artifactType"],
        fields["schemaVersion"]: contract["schemaVersion"],
        fields["families"]: registry[fields["families"]],
        fields["sources"]: [],
    }
    for relative in discovered:
        path = _ordinary_file(root, relative)
        if path is None:
            raise ValueError(f"authority path missing or unsafe: {relative}")
        rendered[fields["sources"]].append(
            {
                source_fields["path"]: relative,
                source_fields["sha256"]: _sha_bytes(path.read_bytes()),
                source_fields["family"]: family_by_path[relative],
                source_fields["units"]: _unit_rows(
                    relative,
                    scan_authority_units(path),
                    assignments=assignments_by_path.get(relative, {}),
                    contract=contract,
                ),
            }
        )
    return rendered


def _python_symbol_exists(path: Path, locator: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == locator
        for node in ast.walk(tree)
    )


def _json_pointer_exists(path: Path, pointer: str) -> bool:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
        for raw in pointer.strip("/").split("/") if pointer else []:
            token = raw.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        return True
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return False


def _test_id_exists(root: Path, test_id: str) -> bool:
    parts = test_id.split(".")
    if len(parts) < 4 or parts[0] != "tests":
        return False
    path = root / ("/".join(parts[:-2]) + ".py")
    if not path.is_file() or path.is_symlink():
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    class_name, method_name = parts[-2:]
    return any(
        isinstance(node, ast.ClassDef)
        and node.name == class_name
        and any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == method_name
            for child in node.body
        )
        for node in tree.body
    )


def validate_registry_snapshot(
    root: Path, registry: dict[str, Any], rules: dict[str, Any]
) -> list[str]:
    contract = rules.get("normativeRuleRegistryContract")
    if not isinstance(contract, dict):
        return ["normativeRuleRegistryContract missing"]

    fields = contract["registryFields"]
    family_fields = contract["familyFields"]
    owner_fields = contract["ownerFields"]
    source_fields = contract["sourceFields"]
    unit_fields = contract["unitFields"]
    errors: list[str] = []
    if set(registry) != set(fields.values()):
        errors.append("registry top-level fields mismatch")
        return errors
    if (
        registry[fields["artifactType"]] != contract["artifactType"]
        or registry[fields["schemaVersion"]] != contract["schemaVersion"]
    ):
        errors.append("registry identity or schema version mismatch")
    families = registry[fields["families"]]
    sources = registry[fields["sources"]]
    if not isinstance(families, dict) or not families:
        errors.append("enforcement families missing")
        return errors
    if not isinstance(sources, list) or not sources:
        errors.append("authority sources missing")
        return errors

    if not re.fullmatch(r"[0-9a-f]{64}", str(contract.get("assignmentDigest", ""))):
        errors.append("reviewed unit assignment digest invalid")
        return errors

    valid_classes = set(contract["enforcementClasses"].values())
    experience_ids = set(rules["historicalExperienceContract"]["experienceIds"])
    critical_roles = set(rules["criticalOutcomeContract"]["requirementIds"])
    for family_id, family in families.items():
        if not isinstance(family_id, str) or not family_id:
            errors.append("family id invalid")
            continue
        if not isinstance(family, dict) or set(family) != set(family_fields.values()):
            errors.append(f"family shape invalid: {family_id}")
            continue
        if family[family_fields["enforcementClass"]] not in valid_classes:
            errors.append(f"family class invalid: {family_id}")
        owner = family[family_fields["owner"]]
        owner_path = (
            _ordinary_file(root, owner.get(owner_fields["path"]))
            if isinstance(owner, dict)
            else None
        )
        owner_kind = owner.get(owner_fields["kind"]) if isinstance(owner, dict) else None
        locator = owner.get(owner_fields["locator"]) if isinstance(owner, dict) else None
        owner_valid = bool(
            isinstance(owner, dict)
            and set(owner) == set(owner_fields.values())
            and owner_kind in set(contract["ownerKinds"].values())
            and owner_path is not None
            and isinstance(locator, str)
            and locator
            and (
                _python_symbol_exists(owner_path, locator)
                if owner_kind == contract["ownerKinds"]["pythonSymbol"]
                else _json_pointer_exists(owner_path, locator)
            )
        )
        if not owner_valid:
            errors.append(f"executable owner invalid: {family_id}")
        for role in ("goodCaseTests", "badCaseTests"):
            test_ids = family[family_fields[role]]
            if not (
                isinstance(test_ids, list)
                and test_ids
                and len(test_ids) == len(set(test_ids))
                and all(
                    isinstance(test_id, str) and _test_id_exists(root, test_id)
                    for test_id in test_ids
                )
            ):
                errors.append(f"{role} invalid: {family_id}")
        if not (
            isinstance(family[family_fields["experienceIds"]], list)
            and family[family_fields["experienceIds"]]
            and set(family[family_fields["experienceIds"]]) <= experience_ids
        ):
            errors.append(f"historical experience binding invalid: {family_id}")
        if not (
            isinstance(family[family_fields["criticalOutcomeRoles"]], list)
            and set(family[family_fields["criticalOutcomeRoles"]]) <= critical_roles
        ):
            errors.append(f"critical outcome binding invalid: {family_id}")

    discovered = discover_authority_paths(root, contract)
    source_paths = [
        item.get(source_fields["path"]) for item in sources if isinstance(item, dict)
    ]
    if source_paths != discovered:
        errors.append("authority path coverage mismatch")
    rule_ids: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != set(source_fields.values()):
            errors.append("authority source shape invalid")
            continue
        relative = source[source_fields["path"]]
        path = _ordinary_file(root, relative)
        if path is None:
            errors.append(f"authority source missing or unsafe: {relative}")
            continue
        if source[source_fields["family"]] not in families:
            errors.append(f"authority family missing: {relative}")
        if source[source_fields["sha256"]] != _sha_bytes(path.read_bytes()):
            errors.append(f"authority source drift: {relative}")
        actual_units = source[source_fields["units"]]
        actual_assignments = {
            unit[unit_fields["identity"]]: unit[unit_fields["family"]]
            for unit in actual_units
            if isinstance(unit, dict)
            and set(unit) == set(unit_fields.values())
            and isinstance(unit.get(unit_fields["identity"]), str)
            and isinstance(unit.get(unit_fields["family"]), str)
        } if isinstance(actual_units, list) else {}
        try:
            expected_units = _unit_rows(
                relative,
                scan_authority_units(path),
                assignments=actual_assignments,
                contract=contract,
            )
        except ValueError:
            expected_units = []
        if actual_units != expected_units:
            errors.append(f"authority unit coverage drift: {relative}")
        if not expected_units:
            errors.append(f"authority source has no indexed units: {relative}")
        rule_ids.extend(row[unit_fields["identity"]] for row in expected_units)
    if len(rule_ids) != len(set(rule_ids)):
        errors.append("normative rule ids are not unique")
    represented_families = {
        unit.get(unit_fields["family"])
        for source in sources
        if isinstance(source, dict)
        for unit in source.get(source_fields["units"], [])
        if isinstance(unit, dict)
    }
    if represented_families != set(families):
        errors.append("unused or unrepresented enforcement family")
    if _assignment_digest(sources, contract) != contract["assignmentDigest"]:
        errors.append("reviewed unit assignment digest drift")
    return errors


def validate_normative_rule_registry(root: Path) -> list[str]:
    rules = _load_object(root / "contracts" / "machine-rules.json")
    contract = rules.get("normativeRuleRegistryContract")
    if not isinstance(contract, dict):
        return ["normativeRuleRegistryContract missing"]
    registry_path = _ordinary_file(root, contract.get("artifactName"))
    if registry_path is None:
        return ["normative rule registry missing"]
    try:
        registry = _load_object(registry_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [f"normative rule registry invalid: {type(exc).__name__}"]
    return validate_registry_snapshot(root, registry, rules)


def registry_digest(registry: dict[str, Any]) -> str:
    return _sha_bytes(_canonical_bytes(registry))
