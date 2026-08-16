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

from produce_meme_template import DeterministicFixtureAdapters, run_production


def main() -> int:
    parser = argparse.ArgumentParser(description="从一张来源网图运行 produce-meme-template P0-P8 工作流")
    parser.add_argument("--request", required=True, type=Path, help="生产请求 JSON")
    parser.add_argument("--output", required=True, type=Path, help="Production Item 输出根目录")
    parser.add_argument("--deterministic-fixture", required=True, type=Path, help="确定性适配器 fixture 目录")
    args = parser.parse_args()

    request_path = args.request.resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    source = Path(request["sourceImage"])
    if not source.is_absolute():
        request["sourceImage"] = str((request_path.parent / source).resolve())
    adapters = DeterministicFixtureAdapters(args.deterministic_fixture)
    fixed_time_path = args.deterministic_fixture / "clock.txt"
    clock = None
    if fixed_time_path.exists():
        fixed = datetime.fromisoformat(fixed_time_path.read_text(encoding="utf-8").strip().replace("Z", "+00:00"))
        clock = lambda: fixed
    result = run_production(request, args.output, adapters, clock=clock)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.outcome == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
