#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from produce_meme_template.release_management import (
    build_release,
    doctor,
    install_release,
    runtime_release_contract,
    write_pin_migration_report,
)


def _git_commit(source: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建、安装、诊断或迁移 produce-meme-template release"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="生成不可变发布包")
    build.add_argument("--source", required=True, type=Path)
    build.add_argument("--dist", required=True, type=Path)
    build.add_argument("--git-commit")
    build.add_argument("--built-at")

    install = commands.add_parser("install", help="安装已验证发布包")
    install.add_argument("--package", required=True, type=Path)
    install.add_argument("--install-root", required=True, type=Path)
    install.add_argument("--expected-release-digest", required=True)

    diagnose = commands.add_parser("doctor", help="诊断源码或安装副本")
    diagnose.add_argument("--runtime", required=True, type=Path)
    diagnose.add_argument("--production-pin", type=Path)

    migrate = commands.add_parser("migrate-pin", help="生成显式 pin 迁移报告")
    migrate.add_argument("--production-item", required=True, type=Path)
    migrate.add_argument("--runtime", required=True, type=Path)

    args = parser.parse_args()
    codes = runtime_release_contract()["errorCodes"]
    try:
        if args.command == "build":
            source = args.source.resolve()
            built_at = (
                datetime.fromisoformat(args.built_at.replace("Z", "+00:00"))
                if args.built_at
                else None
            )
            result = build_release(
                source,
                args.dist,
                git_commit=args.git_commit or _git_commit(source),
                built_at=built_at,
            )
        elif args.command == "install":
            result = install_release(
                args.package,
                args.install_root,
                expected_release_digest=args.expected_release_digest,
            )
        elif args.command == "doctor":
            pin = (
                json.loads(args.production_pin.read_text(encoding="utf-8"))
                if args.production_pin
                else None
            )
            result = doctor(args.runtime, production_pin=pin)
        else:
            result = write_pin_migration_report(
                args.production_item,
                args.runtime,
            )
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        role = (
            "invalidProductionPin"
            if args.command == "doctor"
            else "invalidReleaseMetadata"
        )
        result = {
            "pass": False,
            "errorCode": codes[role],
            "message": type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
