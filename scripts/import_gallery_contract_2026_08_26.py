#!/usr/bin/env python3
"""Create the immutable 2026-08-26 Gallery author-contract snapshot.

The upstream documentation schema omitted ``clothingOwnership`` while the
TypeScript runtime schema and both official examples already consume it.  This
importer starts from the last strict producer snapshot, adds the v1/v2 runtime
union implemented upstream, and records the discrepancy in snapshot metadata.
It refuses to overwrite an existing snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OLD = (
    ROOT
    / "contracts/upstream/gallery-template/agent-template-json-runtime-contract-2026-08-22/gallery-template.schema.json"
)
TARGET_DIR = (
    ROOT
    / "contracts/upstream/gallery-template/agent-template-json-runtime-contract-2026-08-26"
)
UPSTREAM_ROOT = (
    ROOT.parent
    / "memebuy数据/memebuy_monorepo/apps/memebuy-merchant-management"
)
UPSTREAM_DOCUMENT = UPSTREAM_ROOT / "docs/gallery/agent-template-json-runtime-contract.md"
UPSTREAM_SCHEMA = UPSTREAM_ROOT / "docs/gallery/template-import/schema.json"
UPSTREAM_RUNTIME = UPSTREAM_ROOT / "src/modules/gallery/templates/runtime-semantics.ts"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_new(path: Path, value: dict) -> None:
    if path.exists():
        raise FileExistsError(f"immutable snapshot already exists: {path}")
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_schema() -> dict:
    schema = json.loads(OLD.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    legacy_target = copy.deepcopy(defs.pop("targetInstance"))
    legacy_runtime = copy.deepcopy(defs.pop("runtimeSemantics"))

    clothing = {"enum": ["source", "template"]}
    for name in ("oneToOneIdentityBinding", "repeatedIdentityBinding"):
        defs[name]["properties"]["clothingOwnership"] = copy.deepcopy(clothing)

    target_base = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "kind", "role", "region"],
        "properties": {
            "id": {"$ref": "#/$defs/targetId"},
            "role": {"$ref": "#/$defs/nonEmptyText"},
            "region": {"$ref": "#/$defs/nonEmptyText"},
        },
    }
    identity_target = copy.deepcopy(target_base)
    identity_target["properties"].update(
        {
            "kind": {"const": "identity_subject"},
            "groupId": {"$ref": "#/$defs/targetId"},
        }
    )
    content_target = copy.deepcopy(target_base)
    content_target["properties"].update(
        {
            "kind": {"const": "content_element"},
            "groupId": {"$ref": "#/$defs/targetId"},
        }
    )
    group_target = copy.deepcopy(target_base)
    group_target["required"].extend(["memberKind", "minMembers", "maxMembers"])
    group_target["properties"].update(
        {
            "kind": {"const": "identity_group"},
            "memberKind": {"enum": ["person", "pet"]},
            "minMembers": {"type": "integer", "minimum": 1, "maximum": 20},
            "maxMembers": {"type": "integer", "minimum": 1, "maximum": 20},
        }
    )
    dynamic_binding = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "operation",
            "targetIds",
            "bindingPolicy",
            "renderingMode",
            "allowedSourceGrouping",
            "groupToSinglePolicy",
            "clothingOwnership",
        ],
        "properties": {
            "operation": {"const": "replace_identity"},
            "targetIds": {
                "type": "array",
                "prefixItems": [{"$ref": "#/$defs/targetId"}],
                "items": False,
                "minItems": 1,
                "maxItems": 1,
            },
            "bindingPolicy": {"const": "preserve_group"},
            "renderingMode": {"const": "illustration_redraw"},
            "allowedSourceGrouping": {
                "type": "array",
                "prefixItems": [{"const": "group_photo"}],
                "items": False,
                "minItems": 1,
                "maxItems": 1,
            },
            "groupToSinglePolicy": {"const": "reject"},
            "clothingOwnership": copy.deepcopy(clothing),
        },
    }
    defs.update(
        {
            "runtimeTargetInstanceV1": legacy_target,
            "runtimeIdentitySubjectTargetV2": identity_target,
            "runtimeContentElementTargetV2": content_target,
            "runtimeIdentityGroupTargetV2": group_target,
            "runtimeTargetInstanceV2": {
                "oneOf": [
                    {"$ref": "#/$defs/runtimeIdentitySubjectTargetV2"},
                    {"$ref": "#/$defs/runtimeContentElementTargetV2"},
                    {"$ref": "#/$defs/runtimeIdentityGroupTargetV2"},
                ]
            },
            "preserveGroupIdentityBinding": dynamic_binding,
        }
    )
    legacy_runtime["properties"]["targetInstances"]["items"] = {
        "$ref": "#/$defs/runtimeTargetInstanceV1"
    }
    defs["runtimeSemanticsV1"] = legacy_runtime
    v2_runtime = copy.deepcopy(legacy_runtime)
    v2_runtime["properties"]["version"] = {"const": 2}
    v2_runtime["properties"]["targetInstances"]["items"] = {
        "$ref": "#/$defs/runtimeTargetInstanceV2"
    }
    v2_runtime["properties"]["inputBindings"]["additionalProperties"]["oneOf"].insert(
        2, {"$ref": "#/$defs/preserveGroupIdentityBinding"}
    )
    defs["runtimeSemanticsV2"] = v2_runtime
    defs["runtimeSemantics"] = {
        "oneOf": [
            {"$ref": "#/$defs/runtimeSemanticsV1"},
            {"$ref": "#/$defs/runtimeSemanticsV2"},
        ]
    }
    return schema


def main() -> int:
    required = [OLD, UPSTREAM_DOCUMENT, UPSTREAM_SCHEMA, UPSTREAM_RUNTIME]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing contract source: " + ", ".join(missing))
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    schema = build_schema()
    _write_new(TARGET_DIR / "gallery-template.schema.json", schema)
    _write_new(
        TARGET_DIR / "snapshot-metadata.json",
        {
            "artifactType": "upstream-contract-snapshot",
            "contractId": "gallery-template",
            "contractVersion": "agent-template-json-runtime-contract-2026-08-26",
            "source": "研发 CURRENT 作者合同、真实 TypeScript runtime schema 与 template-import/schema.json",
            "sourceEvidence": "2026-08-26 生效；真实 gallery:validate 已通过固定主体与动态群组官方样例",
            "acquisitionCommit": "4504c8a94aa5608f5d2e9e8df30111b983e9e18b",
            "acquiredAt": "2026-08-27T00:00:00+08:00",
            "schemaFile": "gallery-template.schema.json",
            "sourceArtifactSha256": _sha256(
                TARGET_DIR / "gallery-template.schema.json"
            ),
            "sourceDocumentSha256": _sha256(UPSTREAM_DOCUMENT),
            "upstreamImportSchemaSha256": _sha256(UPSTREAM_SCHEMA),
            "upstreamRuntimeSchemaSha256": _sha256(UPSTREAM_RUNTIME),
            "upstreamSchemaDiscrepancies": [
                {
                    "field": "runtimeSemantics.inputBindings.*.clothingOwnership",
                    "documentationSchema": "omitted",
                    "runtimeSchema": "optional source|template",
                    "producerRule": "required for every new replace_identity binding",
                }
            ],
            "compatibility": {
                "consumer": "produce-meme-template",
                "supportedContract": "agent-template-json-runtime-contract-2026-08-26",
                "authorProtocolVersion": 2,
                "inputSchemaVersion": 2,
                "runtimeSemanticsWriteVersion": 2,
                "runtimeSemanticsReadableVersions": [1, 2],
                "formalCoverField": "cover",
                "formalReferenceField": "referenceImage",
                "legacyPromptEnhancementAllowed": False,
                "legacySubjectExtractAllowed": False,
                "legacyArrayInputSchemaAllowedForNewProduction": False,
            },
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
