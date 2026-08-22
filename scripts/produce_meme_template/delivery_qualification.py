from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import (
    load_json,
    pretty_json_bytes,
    sha256_bytes,
)
from .delivery_runtime import (
    _current_finalization_errors,
    _current_item_fact_errors,
    _current_template_data_errors,
)
from .execution_authority import delivery_execution_profile_errors
from .generation_runtime import _current_generation_execution_errors
from .workflow_core import (
    _current_p2_artifact_errors,
    _revisioned_name,
    validate_production_manifest_lineage,
)


def completed_delivery_qualification_errors(
    output_dir: Path,
    manifest: Any,
    expected_record: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    """Replay every persisted fact required to export a completed template."""

    if not isinstance(manifest, dict):
        return ["production manifest must be an object"]
    errors = validate_production_manifest_lineage(output_dir, manifest)
    if (
        manifest.get("state") != rules["resultStates"]["completed"]
        or manifest.get("outcome") != "completed"
    ):
        errors.append("production manifest is not completed")

    execution = rules["productionExecutionContract"]
    manifest_fields = execution["manifestFields"]
    profile_name = execution["artifactName"]
    try:
        profile = load_json(output_dir / profile_name)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        profile = None
        errors.append("production execution profile is unreadable")
    artifacts = manifest.get("artifacts")
    profile_record = (
        artifacts.get(profile_name) if isinstance(artifacts, dict) else None
    )
    formal_record = (
        artifacts.get("gallery-template.json")
        if isinstance(artifacts, dict)
        else None
    )
    profile_sha = (
        sha256_bytes(pretty_json_bytes(profile)) if profile is not None else None
    )
    if (
        manifest.get(manifest_fields["executionMode"])
        != execution["executionModes"]["liveExternal"]
        or not isinstance(profile_record, dict)
        or profile_record.get("sha256") != profile_sha
        or manifest.get(manifest_fields["executionProfileSha256"]) != profile_sha
    ):
        errors.append("production execution profile lineage is invalid")
    errors.extend(delivery_execution_profile_errors(profile, rules))
    if (
        not isinstance(formal_record, dict)
        or formal_record.get("sha256")
        != sha256_bytes(pretty_json_bytes(expected_record))
    ):
        errors.append("formal record does not match production lineage")

    revision = manifest.get("revision")
    try:
        task = load_json(
            output_dir / _revisioned_name("generation-task.json", revision)
        )
        generation = rules["generationExecutionContract"]
        task_fields = generation["taskFields"]
        intent_fields = generation["requestIntentFields"]
        option_fields = generation["requestOptionFields"]
        intent = task[task_fields["requestIntent"]]
        generation_options = {
            option_fields["imageCount"]: intent[intent_fields["imageCount"]],
            option_fields["primaryOutputIndex"]: intent[
                intent_fields["primaryOutputIndex"]
            ],
        }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        generation_options = None
        errors.append("generation options cannot be recovered from current task")

    errors.extend(_current_p2_artifact_errors(manifest))
    if generation_options is not None:
        errors.extend(
            _current_generation_execution_errors(
                output_dir,
                manifest,
                manifest.get("sourceImageSha256"),
                generation_options,
                rules,
            )
        )
    errors.extend(_current_item_fact_errors(output_dir, manifest, rules))
    errors.extend(_current_template_data_errors(output_dir, manifest, rules))
    errors.extend(_current_finalization_errors(output_dir, manifest, rules))
    return errors
