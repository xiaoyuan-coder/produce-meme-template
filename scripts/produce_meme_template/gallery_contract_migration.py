from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class GalleryContractMigrationResult:
    status: str
    migrated: dict[str, Any] | None
    required_decisions: tuple[str, ...]
    errors: tuple[str, ...]


def migrate_gallery_template_to_runtime_v2(
    record: Any,
    clothing_decisions: Mapping[str, str] | None = None,
) -> GalleryContractMigrationResult:
    """Upgrade one fixed-target v1 template without guessing author intent.

    ``clothing_decisions`` is keyed by input id.  Every ``replace_identity``
    binding in v1 requires an explicit ``source`` or ``template`` decision.
    The source record is never mutated.
    """

    if not isinstance(record, dict):
        return GalleryContractMigrationResult(
            status="invalid",
            migrated=None,
            required_decisions=(),
            errors=("template must be an object",),
        )
    runtime = record.get("runtimeSemantics")
    if not isinstance(runtime, dict):
        return GalleryContractMigrationResult(
            status="invalid",
            migrated=None,
            required_decisions=(),
            errors=("runtimeSemantics is required",),
        )
    version = runtime.get("version")
    if version == 2:
        missing = tuple(
            sorted(
                input_id
                for input_id, binding in runtime.get("inputBindings", {}).items()
                if isinstance(binding, dict)
                and binding.get("operation") == "replace_identity"
                and binding.get("clothingOwnership") not in {"source", "template"}
            )
        )
        if missing:
            return GalleryContractMigrationResult(
                status="needs_decision",
                migrated=None,
                required_decisions=missing,
                errors=("v2 identity bindings require explicit clothing ownership",),
            )
        return GalleryContractMigrationResult(
            status="already_current",
            migrated=copy.deepcopy(record),
            required_decisions=(),
            errors=(),
        )
    if version != 1:
        return GalleryContractMigrationResult(
            status="invalid",
            migrated=None,
            required_decisions=(),
            errors=(f"unsupported runtimeSemantics version: {version!r}",),
        )
    targets = runtime.get("targetInstances")
    bindings = runtime.get("inputBindings")
    if not isinstance(targets, list) or not isinstance(bindings, dict):
        return GalleryContractMigrationResult(
            status="invalid",
            migrated=None,
            required_decisions=(),
            errors=("targetInstances and inputBindings are required",),
        )
    if any(
        isinstance(target, dict) and target.get("kind") == "identity_group"
        for target in targets
    ):
        return GalleryContractMigrationResult(
            status="invalid",
            migrated=None,
            required_decisions=(),
            errors=("v1 cannot contain identity_group; dynamic groups require re-authoring",),
        )
    identity_inputs = tuple(
        sorted(
            input_id
            for input_id, binding in bindings.items()
            if isinstance(binding, dict)
            and binding.get("operation") == "replace_identity"
        )
    )
    supplied = dict(clothing_decisions or {})
    missing = tuple(
        input_id
        for input_id in identity_inputs
        if supplied.get(input_id) not in {"source", "template"}
    )
    unknown = tuple(sorted(set(supplied) - set(identity_inputs)))
    if unknown:
        return GalleryContractMigrationResult(
            status="invalid",
            migrated=None,
            required_decisions=missing,
            errors=(
                "clothing decisions reference unknown identity inputs: "
                + ", ".join(unknown),
            ),
        )
    if missing:
        return GalleryContractMigrationResult(
            status="needs_decision",
            migrated=None,
            required_decisions=missing,
            errors=(),
        )
    migrated = copy.deepcopy(record)
    migrated_runtime = migrated["runtimeSemantics"]
    migrated_runtime["version"] = 2
    for input_id in identity_inputs:
        migrated_runtime["inputBindings"][input_id]["clothingOwnership"] = supplied[
            input_id
        ]
    return GalleryContractMigrationResult(
        status="migrated",
        migrated=migrated,
        required_decisions=(),
        errors=(),
    )
