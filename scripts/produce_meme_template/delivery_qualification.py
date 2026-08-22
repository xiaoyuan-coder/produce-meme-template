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
    current_delivery_fact_qualification_errors,
)
from .execution_authority import (
    delivery_execution_profile_errors,
    production_execution_profile_errors,
)
from .generation_runtime import current_generation_qualification_errors
from .workflow_core import (
    current_workflow_qualification_errors,
    revisioned_artifact_name,
)


def _production_qualification_errors(
    output_dir: Path,
    manifest: Any,
    expected_record: dict[str, Any],
    rules: dict[str, Any],
    *,
    require_completed: bool,
    require_delivery: bool,
) -> list[str]:
    """Replay all persisted production facts through the formal projection."""

    if not isinstance(manifest, dict):
        return ["production manifest must be an object"]
    errors = current_workflow_qualification_errors(output_dir, manifest)
    if require_completed and (
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
        not isinstance(profile_record, dict)
        or profile_record.get("sha256") != profile_sha
        or manifest.get(manifest_fields["executionProfileSha256"]) != profile_sha
        or not isinstance(profile, dict)
        or profile.get(execution["profileFields"]["executionMode"])
        != manifest.get(manifest_fields["executionMode"])
    ):
        errors.append("production execution profile lineage is invalid")
    if require_delivery:
        errors.extend(delivery_execution_profile_errors(profile, rules))
    else:
        errors.extend(production_execution_profile_errors(profile, rules))
    if (
        not isinstance(formal_record, dict)
        or formal_record.get("sha256")
        != sha256_bytes(pretty_json_bytes(expected_record))
    ):
        errors.append("formal record does not match production lineage")

    revision = manifest.get("revision")
    try:
        task = load_json(
            output_dir / revisioned_artifact_name("generation-task.json", revision)
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

    if generation_options is not None:
        errors.extend(
            current_generation_qualification_errors(
                output_dir,
                manifest,
                manifest.get("sourceImageSha256"),
                generation_options,
                rules,
            )
        )
    errors.extend(
        current_delivery_fact_qualification_errors(output_dir, manifest, rules)
    )
    return errors


def p8_completion_qualification_errors(
    output_dir: Path,
    manifest: Any,
    expected_record: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    """Replay every production fact before the first P8 completion transition."""

    return _production_qualification_errors(
        output_dir,
        manifest,
        expected_record,
        rules,
        require_completed=False,
        require_delivery=False,
    )


def completed_delivery_qualification_errors(
    output_dir: Path,
    manifest: Any,
    expected_record: dict[str, Any],
    rules: dict[str, Any],
) -> list[str]:
    """Replay every persisted fact required to export a completed template."""

    return _production_qualification_errors(
        output_dir,
        manifest,
        expected_record,
        rules,
        require_completed=True,
        require_delivery=True,
    )
