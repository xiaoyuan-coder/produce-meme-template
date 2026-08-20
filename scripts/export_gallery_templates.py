#!/usr/bin/env python3
"""Export formal Gallery Template records as one key-named JSON file each."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from produce_meme_template.artifacts import (  # noqa: E402
    pretty_json_bytes,
    sha256_bytes,
    sha256_file,
)
from produce_meme_template.template_compiler import (  # noqa: E402
    formal_template_contract_valid,
)
from produce_meme_template.workflow_core import (  # noqa: E402
    GALLERY_SCHEMA,
    MACHINE_RULES,
)


KEY_PATTERN = re.compile(GALLERY_SCHEMA["properties"]["key"]["pattern"])


class ExportError(ValueError):
    """Raised when a formal-record export would be unsafe or ambiguous."""


def _load_records(source: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError(f"无法读取来源 JSON：{source}: {error}") from error

    if isinstance(value, dict):
        records = [value]
    elif isinstance(value, list) and value:
        records = value
    else:
        raise ExportError("来源必须是一条正式模板对象或非空正式模板数组。")

    if not all(isinstance(record, dict) for record in records):
        raise ExportError("来源数组中的每一项都必须是 JSON 对象。")

    keys: list[str] = []
    for index, record in enumerate(records):
        key = record.get("key")
        if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
            raise ExportError(f"第 {index + 1} 条记录缺少合法 key。")
        if Path(key).name != key or "/" in key or "\\" in key:
            raise ExportError(f"模板 key 不能形成路径：{key}")
        if not formal_template_contract_valid(record, MACHINE_RULES):
            raise ExportError(f"模板 {key} 未通过当前正式 Gallery 合同。")
        keys.append(key)

    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ExportError("来源包含重复 key：" + "、".join(duplicates))

    return records


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def export_gallery_templates(
    source: Path,
    output_dir: Path,
    *,
    manifest_path: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate and export one formal record per ``<key>.json`` file."""
    source = source.resolve()
    output_dir = output_dir.resolve()
    records = _load_records(source)
    expected_names = {f"{record['key']}.json" for record in records}

    manifest_path = manifest_path.resolve()
    if manifest_path == output_dir or manifest_path.is_relative_to(output_dir):
        raise ExportError("交付清单必须位于单模板 JSON 数据目录之外。")

    if output_dir.exists() and not output_dir.is_dir():
        raise ExportError(f"输出路径不是目录：{output_dir}")

    unexpected = []
    if output_dir.exists():
        unexpected = sorted(
            path.name
            for path in output_dir.iterdir()
            if not path.is_file() or path.name not in expected_names
        )
    if unexpected:
        raise ExportError(
            "单模板 JSON 目录包含本次交付范围外的内容：" + "、".join(unexpected)
        )

    payloads = {
        f"{record['key']}.json": pretty_json_bytes(record)
        for record in records
    }
    conflicts = []
    for name, payload in payloads.items():
        target = output_dir / name
        if target.exists() and target.read_bytes() != payload and not overwrite:
            conflicts.append(name)
    if conflicts:
        raise ExportError(
            "目标文件已有不同内容；确认后使用 --overwrite："
            + "、".join(sorted(conflicts))
        )

    files = [
        {
            "key": record["key"],
            "fileName": f"{record['key']}.json",
            "sha256": sha256_bytes(payloads[f"{record['key']}.json"]),
        }
        for record in records
    ]
    manifest = {
        "artifactType": "gallery-template-record-export",
        "schemaVersion": "1.0.0",
        "source": str(source),
        "sourceSha256": sha256_file(source),
        "recordCount": len(records),
        "keys": [record["key"] for record in records],
        "outputDirectory": str(output_dir),
        "files": files,
        "recordsSha256": sha256_bytes(
            b"".join(payloads[file["fileName"]] for file in files)
        ),
    }
    manifest_payload = pretty_json_bytes(manifest)
    if (
        manifest_path.exists()
        and manifest_path.read_bytes() != manifest_payload
        and not overwrite
    ):
        raise ExportError(
            f"交付清单已有不同内容；确认后使用 --overwrite：{manifest_path}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        target = output_dir / name
        if not target.exists() or target.read_bytes() != payload:
            _atomic_write(target, payload)

    if not manifest_path.exists() or manifest_path.read_bytes() != manifest_payload:
        _atomic_write(manifest_path, manifest_payload)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace conflicting key files and manifest after explicit review",
    )
    args = parser.parse_args()
    try:
        manifest = export_gallery_templates(
            args.source,
            args.output_dir,
            manifest_path=args.manifest,
            overwrite=args.overwrite,
        )
    except ExportError as error:
        print(f"export blocked: {error}", file=sys.stderr)
        return 2
    print(
        f"exported {manifest['recordCount']} records to "
        f"{manifest['outputDirectory']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
