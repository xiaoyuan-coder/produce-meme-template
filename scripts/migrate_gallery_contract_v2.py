#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from produce_meme_template.gallery_contract_migration import (
    migrate_gallery_template_to_runtime_v2,
)


def _records(path: Path) -> list[tuple[str, dict[str, Any]]]:
    paths = sorted(path.glob("*.json")) if path.is_dir() else [path]
    records: list[tuple[str, dict[str, Any]]] = []
    for item in paths:
        payload = json.loads(item.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            for index, record in enumerate(payload):
                records.append((f"{item.stem}-{index + 1}", record))
        else:
            records.append((item.stem, payload))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit or migrate Gallery runtimeSemantics v1 templates to v2"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and args.output is None:
        parser.error("--apply requires --output")
    decisions = (
        json.loads(args.decisions.read_text(encoding="utf-8"))
        if args.decisions
        else {}
    )
    report: list[dict[str, Any]] = []
    exit_code = 0
    if args.apply:
        args.output.mkdir(parents=True, exist_ok=True)
    for fallback_key, record in _records(args.input):
        template_key = (
            record.get("key", fallback_key)
            if isinstance(record, dict)
            else fallback_key
        )
        result = migrate_gallery_template_to_runtime_v2(
            record,
            decisions.get(template_key, {}) if isinstance(decisions, dict) else {},
        )
        item = {
            "key": template_key,
            "status": result.status,
            "requiredClothingDecisions": list(result.required_decisions),
            "errors": list(result.errors),
        }
        if result.status in {"invalid", "needs_decision"}:
            exit_code = 2
        elif args.apply and result.migrated is not None:
            output_path = args.output / f"{template_key}.json"
            if output_path.exists():
                item["status"] = "output_exists"
                item["errors"] = ["refusing to overwrite existing migration output"]
                exit_code = 2
            else:
                output_path.write_text(
                    json.dumps(result.migrated, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                item["output"] = str(output_path)
        report.append(item)
    print(json.dumps({"items": report}, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
