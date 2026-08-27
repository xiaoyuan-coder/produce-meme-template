#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from tempfile import NamedTemporaryFile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_meme_template.normative_registry import (
    refresh_registry,
    validate_normative_rule_registry,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查 Skill 全量规范条款的代码所有者与红绿证据"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--render", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.check:
        errors = validate_normative_rule_registry(root)
        if errors:
            print("normative registry FAIL")
            for error in errors:
                print(f"- {error}")
            return 1
        registry = json.loads(
            (root / "contracts" / "normative-rule-registry.json").read_text(
                encoding="utf-8"
            )
        )
        count = sum(len(source["units"]) for source in registry["sources"])
        print(
            f"normative registry PASS: {len(registry['sources'])} sources, "
            f"{count} units, {len(registry['families'])} enforcement families"
        )
        return 0
    rules = json.loads(
        (root / "contracts" / "machine-rules.json").read_text(encoding="utf-8")
    )
    registry = json.loads(
        (root / "contracts" / "normative-rule-registry.json").read_text(
            encoding="utf-8"
        )
    )
    rendered = json.dumps(
        refresh_registry(root, registry, rules),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    if args.write:
        target = root / rules["normativeRuleRegistryContract"]["artifactName"]
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, delete=False
        ) as temporary:
            temporary.write(rendered)
            temporary_path = Path(temporary.name)
        temporary_path.replace(target)
        print(f"updated {target.relative_to(root)}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
