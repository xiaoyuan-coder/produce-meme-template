#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from produce_meme_template.gallery_contract_migration import (
    migrate_gallery_template_to_runtime_v2,
)


ROOT = Path(__file__).resolve().parents[1]


def _rules() -> dict[str, Any]:
    return json.loads(
        (ROOT / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
    )


def _records(
    path: Path,
    migration_contract: dict[str, Any],
) -> list[tuple[str, Any]]:
    paths = sorted(path.glob("*.json")) if path.is_dir() else [path]
    records: list[tuple[str, Any]] = []
    bundle_fields = migration_contract["bundleFields"]
    for item in paths:
        payload = json.loads(item.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            for index, record in enumerate(payload):
                records.append((f"{item.stem}-{index + 1}", record))
        elif (
            isinstance(payload, dict)
            and bundle_fields["templates"] in payload
        ):
            templates = payload.get(bundle_fields["templates"])
            version = payload.get(bundle_fields["version"])
            if (
                not isinstance(version, int)
                or isinstance(version, bool)
                or version not in set(migration_contract["readableBundleVersions"])
                or not isinstance(templates, list)
            ):
                raise ValueError(f"invalid Gallery v2 bundle: {item}")
            for index, record in enumerate(templates):
                records.append((f"{item.stem}-{index + 1}", record))
        else:
            records.append((item.stem, payload))
    return records


def _safe_output_path(
    output_root: Path,
    template_key: Any,
    migration_contract: dict[str, Any],
) -> Path | None:
    if not (
        isinstance(template_key, str)
        and re.fullmatch(migration_contract["templateKeyPattern"], template_key)
    ):
        return None
    candidate = output_root / (
        template_key + migration_contract["outputExtension"]
    )
    try:
        candidate.relative_to(output_root)
    except ValueError:
        return None
    return candidate


def _write_create_once(path: Path, payload: dict[str, Any]) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        return False
    try:
        data = (
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or migrate Gallery runtimeSemantics v1 templates to v2"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rules = _rules()
    migration_contract = rules["galleryContractMigrationContract"]
    statuses = migration_contract["statuses"]
    report_fields = migration_contract["reportFields"]
    template_key_field = rules["formalProjection"]["topLevel"]["templateKey"]
    if args.apply and args.output is None:
        parser.error("--apply requires --output")
    decisions = (
        json.loads(args.decisions.read_text(encoding="utf-8"))
        if args.decisions
        else {}
    )
    report: list[dict[str, Any]] = []
    exit_code = 0
    output_root: Path | None = None
    if args.apply:
        if args.output.is_symlink():
            parser.error("--output must not be a symlink")
        args.output.mkdir(parents=True, exist_ok=True)
        output_root = args.output.resolve()
    try:
        records = _records(args.input, migration_contract)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    report_fields["items"]: [
                        {
                            report_fields["key"]: args.input.stem,
                            report_fields["status"]: statuses["invalid"],
                            report_fields["requiredClothingDecisions"]: [],
                            report_fields["errors"]: [str(exc)],
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    for fallback_key, record in records:
        template_key = (
            record.get(template_key_field, fallback_key)
            if isinstance(record, dict)
            else fallback_key
        )
        decision_values = (
            decisions.get(template_key, {})
            if isinstance(decisions, dict) and isinstance(template_key, str)
            else {}
        )
        result = migrate_gallery_template_to_runtime_v2(
            record,
            decision_values,
            rules=rules,
        )
        item = {
            report_fields["key"]: template_key,
            report_fields["status"]: result.status,
            report_fields["requiredClothingDecisions"]: list(
                result.required_decisions
            ),
            report_fields["errors"]: list(result.errors),
        }
        if result.status in {statuses["invalid"], statuses["needsDecision"]}:
            exit_code = 2
        elif args.apply and result.migrated is not None:
            output_path = _safe_output_path(
                output_root,
                template_key,
                migration_contract,
            )
            if output_path is None:
                item[report_fields["status"]] = statuses["invalid"]
                item[report_fields["errors"]] = [
                    "template key cannot name an isolated migration output"
                ]
                exit_code = 2
            elif not _write_create_once(output_path, result.migrated):
                item[report_fields["status"]] = statuses["outputExists"]
                item[report_fields["errors"]] = [
                    "refusing to overwrite existing migration output"
                ]
                exit_code = 2
            else:
                item[report_fields["output"]] = str(output_path)
        report.append(item)
    print(
        json.dumps(
            {report_fields["items"]: report},
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
