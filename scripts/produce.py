#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from produce_meme_template import (
    BatchProductionResult,
    DeterministicFixtureAdapters,
    run_production,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行单图或批量 produce-meme-template P0-P8 工作流")
    parser.add_argument("--request", required=True, type=Path, help="生产请求 JSON")
    parser.add_argument("--output", required=True, type=Path, help="Production Item 输出根目录")
    parser.add_argument("--deterministic-fixture", required=True, type=Path, help="确定性适配器 fixture 目录")
    args = parser.parse_args()

    request_path = args.request.resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    rules = json.loads(
        (SCRIPT_DIR.parent / "contracts" / "machine-rules.json").read_text(
            encoding="utf-8"
        )
    )
    items_field = rules["batchProductionContract"]["requestFields"]["items"]
    requests = request.get(items_field) if isinstance(request.get(items_field), list) else [request]
    for item in requests:
        if not isinstance(item, dict) or "sourceImage" not in item:
            continue
        source = Path(item["sourceImage"])
        if not source.is_absolute():
            item["sourceImage"] = str((request_path.parent / source).resolve())
    adapters = DeterministicFixtureAdapters(args.deterministic_fixture)
    fixed_time_path = args.deterministic_fixture / "clock.txt"
    clock = None
    if fixed_time_path.exists():
        fixed = datetime.fromisoformat(fixed_time_path.read_text(encoding="utf-8").strip().replace("Z", "+00:00"))
        clock = lambda: fixed
    result = run_production(request, args.output, adapters, clock=clock)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    if isinstance(result, BatchProductionResult):
        return 0 if result.items and all(
            item.outcome == "completed" for item in result.items
        ) else 1
    return 0 if result.outcome == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
