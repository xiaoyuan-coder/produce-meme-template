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
    TemplateTestPreparationResult,
    prepare_template_test,
    run_production,
)


def _fixture_clock(fixture: Path):
    fixed_time_path = fixture / "clock.txt"
    if not fixed_time_path.exists():
        return None
    fixed = datetime.fromisoformat(
        fixed_time_path.read_text(encoding="utf-8")
        .strip()
        .replace("Z", "+00:00")
    )
    return lambda: fixed


def _run_t1(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="把现成正式模板 JSON 编译为 Codex 内置生图 T1 执行包"
    )
    parser.add_argument("--request", required=True, type=Path, help="T1 请求 JSON")
    parser.add_argument("--output", required=True, type=Path, help="独立 T1 输出根目录")
    parser.add_argument(
        "--deterministic-fixture", required=True, type=Path, help="仅用于本地获取测试 reference 的 fixture"
    )
    args = parser.parse_args(argv)
    request_path = args.request.resolve()
    rules = json.loads(
        (SCRIPT_DIR.parent / "contracts" / "machine-rules.json").read_text(
            encoding="utf-8"
        )
    )
    template_field = rules["templateTestContract"]["requestFields"][
        "templateJsonPath"
    ]
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        request = None
    template_path = request.get(template_field) if isinstance(request, dict) else None
    if isinstance(template_path, str) and not Path(template_path).is_absolute():
        request[template_field] = str((request_path.parent / template_path).resolve())
    cases_field = rules["templateTestContract"]["requestFields"]["cases"]
    image_inputs_field = rules["templateTestContract"]["codexBuiltinExecution"][
        "imageInputField"
    ]
    for case in request.get(cases_field, []) if isinstance(request, dict) else []:
        image_inputs = case.get(image_inputs_field) if isinstance(case, dict) else None
        if not isinstance(image_inputs, dict):
            continue
        for slot_id, raw_path in list(image_inputs.items()):
            if isinstance(raw_path, str) and not Path(raw_path).is_absolute():
                image_inputs[slot_id] = str(
                    (request_path.parent / raw_path).resolve()
                )
    adapters = DeterministicFixtureAdapters(args.deterministic_fixture)
    result: TemplateTestPreparationResult = prepare_template_test(
        request,
        args.output,
        adapters,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.outcome == "prepared" else 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "t1":
        return _run_t1(arguments[1:])
    parser = argparse.ArgumentParser(
        description="运行单图或批量 produce-meme-template 四阶段可恢复工作流"
    )
    parser.add_argument("--request", required=True, type=Path, help="生产请求 JSON")
    parser.add_argument("--output", required=True, type=Path, help="Production Item 输出根目录")
    parser.add_argument("--deterministic-fixture", required=True, type=Path, help="确定性适配器 fixture 目录")
    parser.add_argument(
        "--stage",
        default="4",
        help="执行到第 1/2/3/4 阶段；也接受 replacement/image/data/final",
    )
    args = parser.parse_args(arguments)

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
    clock = _fixture_clock(args.deterministic_fixture)
    result = run_production(
        request,
        args.output,
        adapters,
        clock=clock,
        stage=args.stage,
        execution_mode=rules["productionExecutionContract"]["executionModes"][
            "recordedReplay"
        ],
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    if isinstance(result, BatchProductionResult):
        return 0 if result.items and all(
            item.outcome == "completed" for item in result.items
        ) else 1
    return 0 if result.outcome == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
