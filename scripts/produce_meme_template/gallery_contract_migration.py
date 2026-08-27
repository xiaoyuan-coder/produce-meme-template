from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class GalleryContractMigrationResult:
    status: str
    migrated: dict[str, Any] | None
    required_decisions: tuple[str, ...]
    errors: tuple[str, ...]


def _rules() -> dict[str, Any]:
    return json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "machine-rules.json"
        ).read_text(encoding="utf-8")
    )


def _validation_errors(
    record: dict[str, Any],
    rules: dict[str, Any],
    *,
    require_current: bool,
) -> tuple[str, ...]:
    from .template_compiler import _validate_final

    report = _validate_final(record, rules, require_current=require_current)
    if report.get("pass") is True:
        return ()
    errors: list[str] = []
    for key, value in report.items():
        if key == "pass" or not key.endswith("Errors"):
            continue
        if isinstance(value, list):
            errors.extend(str(item) for item in value if str(item).strip())
    return tuple(errors or ("template failed Gallery contract validation",))


def migrate_gallery_template_to_runtime_v2(
    record: Any,
    clothing_decisions: Mapping[str, str] | None = None,
    *,
    rules: Mapping[str, Any] | None = None,
) -> GalleryContractMigrationResult:
    """Upgrade one fixed-target v1 template without guessing author intent.

    ``clothing_decisions`` is keyed by input id.  Every ``replace_identity``
    binding in v1 requires an explicit ``source`` or ``template`` decision.
    The source record is never mutated.
    """

    machine_rules = copy.deepcopy(dict(rules)) if rules is not None else _rules()
    migration = machine_rules["galleryContractMigrationContract"]
    statuses = migration["statuses"]
    runtime_contract = machine_rules["runtimeSemanticsContract"]
    formal_fields = machine_rules["formalProjection"]["topLevel"]
    runtime_field = formal_fields["runtimeSemantics"]
    runtime_fields = runtime_contract["fields"]
    version_field = runtime_fields["version"]
    targets_field = runtime_fields["targetInstances"]
    bindings_field = runtime_fields["inputBindings"]
    target_kind_field = runtime_contract["targetInstanceFields"]["kind"]
    identity_group_kind = runtime_contract["targetKinds"]["identityGroup"]
    binding_fields = runtime_contract["inputBindingFields"]
    operation_field = binding_fields["operation"]
    clothing_field = binding_fields["clothingOwnership"]
    replace_identity = runtime_contract["operations"]["replaceIdentity"]
    clothing_values = set(runtime_contract["clothingOwnershipValues"].values())
    current_version = runtime_contract["version"]
    legacy_versions = [
        version
        for version in runtime_contract["readableVersions"]
        if version != current_version
    ]
    legacy_version = legacy_versions[0] if len(legacy_versions) == 1 else None

    if not isinstance(record, dict):
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=("template must be an object",),
        )
    runtime = record.get(runtime_field)
    if not isinstance(runtime, dict):
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=(f"{runtime_field} is required",),
        )
    version = runtime.get(version_field)
    if version == current_version:
        missing = tuple(
            sorted(
                input_id
                for input_id, binding in runtime.get(bindings_field, {}).items()
                if isinstance(binding, dict)
                and binding.get(operation_field) == replace_identity
                and binding.get(clothing_field) not in clothing_values
            )
        )
        validation_record = copy.deepcopy(record)
        validation_bindings = validation_record[runtime_field].get(bindings_field, {})
        fallback_clothing = next(iter(clothing_values))
        for input_id in missing:
            validation_bindings[input_id][clothing_field] = fallback_clothing
        validation_errors = _validation_errors(
            validation_record,
            machine_rules,
            require_current=True,
        )
        if validation_errors:
            return GalleryContractMigrationResult(
                status=statuses["invalid"],
                migrated=None,
                required_decisions=missing,
                errors=validation_errors,
            )
        if missing:
            return GalleryContractMigrationResult(
                status=statuses["needsDecision"],
                migrated=None,
                required_decisions=missing,
                errors=("v2 identity bindings require explicit clothing ownership",),
            )
        return GalleryContractMigrationResult(
            status=statuses["alreadyCurrent"],
            migrated=copy.deepcopy(record),
            required_decisions=(),
            errors=(),
        )
    if version != legacy_version:
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=(f"unsupported runtimeSemantics version: {version!r}",),
        )
    legacy_errors = _validation_errors(
        record,
        machine_rules,
        require_current=False,
    )
    if legacy_errors:
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=legacy_errors,
        )
    targets = runtime.get(targets_field)
    bindings = runtime.get(bindings_field)
    if not isinstance(targets, list) or not isinstance(bindings, dict):
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=("targetInstances and inputBindings are required",),
        )
    if any(
        isinstance(target, dict)
        and target.get(target_kind_field) == identity_group_kind
        for target in targets
    ):
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=("v1 cannot contain identity_group; dynamic groups require re-authoring",),
        )
    identity_inputs = tuple(
        sorted(
            input_id
            for input_id, binding in bindings.items()
            if isinstance(binding, dict)
            and binding.get(operation_field) == replace_identity
        )
    )
    supplied = dict(clothing_decisions or {})
    missing = tuple(
        input_id
        for input_id in identity_inputs
        if supplied.get(input_id) not in clothing_values
    )
    unknown = tuple(sorted(set(supplied) - set(identity_inputs)))
    if unknown:
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=missing,
            errors=(
                "clothing decisions reference unknown identity inputs: "
                + ", ".join(unknown),
            ),
        )
    if missing:
        return GalleryContractMigrationResult(
            status=statuses["needsDecision"],
            migrated=None,
            required_decisions=missing,
            errors=(),
        )
    migrated = copy.deepcopy(record)
    migrated_runtime = migrated[runtime_field]
    migrated_runtime[version_field] = current_version
    for input_id in identity_inputs:
        migrated_runtime[bindings_field][input_id][clothing_field] = supplied[input_id]
    migrated_errors = _validation_errors(
        migrated,
        machine_rules,
        require_current=True,
    )
    if migrated_errors:
        return GalleryContractMigrationResult(
            status=statuses["invalid"],
            migrated=None,
            required_decisions=(),
            errors=migrated_errors,
        )
    return GalleryContractMigrationResult(
        status=statuses["migrated"],
        migrated=migrated,
        required_decisions=(),
        errors=(),
    )
