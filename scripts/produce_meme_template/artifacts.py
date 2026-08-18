"""Canonical JSON and digest primitives shared by persisted artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def compact_json_line_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def pretty_json_bytes(value: Any, *, sort_keys: bool = False) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=sort_keys)
        + "\n"
    ).encode("utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_object(path: Path) -> dict[str, Any]:
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def load_json_object_or_none(path: Path) -> dict[str, Any] | None:
    try:
        return load_json_object(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())
