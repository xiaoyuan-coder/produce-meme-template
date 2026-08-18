from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .artifacts import load_json as _load_json
from .batch_policy import (
    _isolated_output_dir,
    _normalize_shared_policy,
    _production_request_errors,
    _resolve_shared_policy,
    _shared_policy_errors,
)
from .generation_runtime import image_bytes_match_output_format
from .production_runtime import _run_single_production
from .template_compiler import (
    _formal_projection,
    _validate_final,
    formal_template_contract_valid,
)
from .workflow_core import (
    GALLERY_SCHEMA_PATH,
    RULES_PATH,
    BatchProductionResult,
    ProductionResult,
    WorkflowAdapters,
    WorkflowStop,
    _stop,
    validate_production_manifest_lineage,
)


def _run_batch_item(
    request: dict[str, Any],
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None,
    prepared_source_analysis: dict[str, Any] | None = None,
    shared_policy_resolution: dict[str, Any] | None = None,
    preparation_stop: WorkflowStop | None = None,
) -> ProductionResult:
    try:
        return _run_single_production(
            request,
            output_root,
            adapters,
            clock=clock,
            prepared_source_analysis=prepared_source_analysis,
            shared_policy_resolution=shared_policy_resolution,
            preparation_stop=preparation_stop,
        )
    except (KeyError, OSError, TypeError, ValueError):
        rules = _load_json(RULES_PATH)
        item_id = str(request.get("productionItemId", "invalid-production-item"))
        return ProductionResult(
            "needs_input",
            item_id,
            rules["resultStates"]["needs_input"],
            Path(output_root).resolve() / item_id,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="批量中该 Production Item 的输入无法读取或解析。",
        )

def run_production(
    request: Any,
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ProductionResult | BatchProductionResult:
    """Run one Production Item or split a batch into independent P0-P8 items."""

    rules = _load_json(RULES_PATH)
    if not isinstance(request, dict):
        return ProductionResult(
            "needs_input",
            "invalid-production-item",
            rules["resultStates"]["needs_input"],
            Path(output_root).resolve(),
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="生产请求必须是对象。",
        )
    contract = rules["batchProductionContract"]
    request_fields = contract["requestFields"]
    batch_field = request_fields["batchIdentity"]
    items_field = request_fields["items"]
    shared_policy_field = request_fields["sharedPolicy"]
    if batch_field not in request and items_field not in request:
        return _run_single_production(
            request, output_root, adapters, clock=clock
        )
    batch_id = request.get(batch_field)
    raw_items = request.get(items_field)
    item_ids = [
        item.get("productionItemId")
        for item in raw_items
        if isinstance(item, dict)
    ] if isinstance(raw_items, list) else []
    envelope_fields = {batch_field, items_field, shared_policy_field}
    if (
        not isinstance(batch_id, str)
        or re.fullmatch(rules["identifiers"]["productionItemIdPattern"], batch_id)
        is None
        or set(request) - envelope_fields
        or not isinstance(raw_items, list)
        or not contract["minimumItems"] <= len(raw_items) <= contract["maximumItems"]
        or not all(isinstance(item, dict) for item in raw_items)
        or not all(
            isinstance(item_id, str)
            and re.fullmatch(
                rules["identifiers"]["productionItemIdPattern"], item_id
            )
            is not None
            for item_id in item_ids
        )
        or len(item_ids) != len(set(item_ids))
    ):
        return BatchProductionResult(
            batch_id=batch_id if isinstance(batch_id, str) and batch_id else "invalid-batch",
            items=(),
            shared_policy_applied=False,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="批量请求的标识符或 Production Item 列表无效。",
        )
    shared_policy = request.get(shared_policy_field)
    if shared_policy is None:
        results = tuple(
            _run_batch_item(
                item, output_root, adapters, clock=clock
            )
            for item in raw_items
        )
        return BatchProductionResult(
            batch_id=batch_id,
            items=results,
            shared_policy_applied=False,
        )
    policy_errors = _shared_policy_errors(shared_policy, set(item_ids), rules)
    if policy_errors:
        return BatchProductionResult(
            batch_id=batch_id,
            items=(),
            shared_policy_applied=False,
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="共享批次策略预检失败：" + "；".join(policy_errors),
        )
    normalized_policy = _normalize_shared_policy(shared_policy, rules)
    output_root_path = Path(output_root).resolve()
    schema = _load_json(GALLERY_SCHEMA_PATH)
    invalid_item_ids = {
        item["productionItemId"]
        for item in raw_items
        if _production_request_errors(item, rules, schema)
        or _isolated_output_dir(
            output_root_path,
            item["productionItemId"],
        )
        is None
    }
    try:
        (
            effective_requests,
            analyses,
            resolutions,
            preparation_failures,
        ) = _resolve_shared_policy(
            batch_id,
            raw_items,
            normalized_policy,
            output_root_path,
            adapters,
            rules,
            invalid_item_ids,
        )
    except WorkflowStop as stop:
        return BatchProductionResult(
            batch_id=batch_id,
            items=(),
            shared_policy_applied=True,
            error_code=stop.error_code,
            message=stop.message,
        )
    scope = set(
        normalized_policy[contract["sharedPolicyFields"]["scope"]]
    )
    item_results: list[ProductionResult] = []
    for item in raw_items:
        item_id = item["productionItemId"]
        if item_id not in scope:
            item_results.append(
                _run_batch_item(
                    item, output_root, adapters, clock=clock
                )
            )
            continue
        if item_id in preparation_failures:
            failed_request = effective_requests.get(item_id, item)
            item_results.append(
                _run_batch_item(
                    failed_request,
                    output_root,
                    adapters,
                    clock=clock,
                    preparation_stop=preparation_failures[item_id],
                )
            )
            continue
        if item_id not in effective_requests or item_id not in resolutions:
            item_results.append(
                _run_batch_item(
                    item,
                    output_root,
                    adapters,
                    clock=clock,
                    preparation_stop=_stop(
                        rules,
                        "blocked",
                        "noCompatibleReplacement",
                        "共享批次策略没有为该生产项分配兼容值。",
                        {"productionItemId": item_id},
                    ),
                )
            )
            continue
        item_results.append(
            _run_batch_item(
                effective_requests[item_id],
                output_root,
                adapters,
                clock=clock,
                prepared_source_analysis=analyses.get(item_id),
                shared_policy_resolution=resolutions[item_id],
            )
        )
    return BatchProductionResult(
        batch_id=batch_id,
        items=tuple(item_results),
        shared_policy_applied=True,
    )
