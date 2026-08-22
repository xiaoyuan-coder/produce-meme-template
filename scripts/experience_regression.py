#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from produce_meme_template.experience_regression import run_experience_regression


def main() -> int:
    parser = argparse.ArgumentParser(
        description="执行机器合同声明的全部历史经验回归并生成绑定版本 pin 的报告"
    )
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run_experience_regression(args.runtime, args.output)
        rules = json.loads(
            (args.runtime / "contracts" / "machine-rules.json").read_text(
                encoding="utf-8"
            )
        )
        pass_field = rules["historicalExperienceContract"]["reportFields"][
            "pass"
        ]
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"pass": False, "message": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report[pass_field] else 1


if __name__ == "__main__":
    raise SystemExit(main())
