from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
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
    target_stage: int = 4,
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
            target_stage=target_stage,
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


def _map_batch_items(
    items: list[dict[str, Any]],
    operation: Callable[[dict[str, Any]], ProductionResult],
    rules: dict[str, Any],
) -> tuple[ProductionResult, ...]:
    max_workers = min(
        len(items),
        rules["batchProductionContract"]["executionPolicy"][
            "defaultMaxConcurrency"
        ],
    )
    if max_workers <= 1:
        return tuple(operation(item) for item in items)
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="meme-production",
    ) as executor:
        return tuple(executor.map(operation, items))


def _run_batch_with_stage_barriers(
    items: list[dict[str, Any]],
    operation: Callable[[dict[str, Any], int], ProductionResult],
    rules: dict[str, Any],
    target_stage: int,
) -> tuple[ProductionResult, ...]:
    """Finish every eligible item at one major stage before starting the next."""

    if not rules["batchProductionContract"]["executionPolicy"][
        "majorStageBarrierBeforeNextStage"
    ]:
        return _map_batch_items(
            items,
            lambda item: operation(item, target_stage),
            rules,
        )
    results: list[ProductionResult | None] = [None] * len(items)
    for stage_number in range(1, target_stage + 1):
        eligible_indices = [
            index
            for index, result in enumerate(results)
            if result is None or result.outcome == "completed"
        ]
        if not eligible_indices:
            break
        stage_items = [items[index] for index in eligible_indices]
        stage_results = _map_batch_items(
            stage_items,
            lambda item: operation(item, stage_number),
            rules,
        )
        for index, result in zip(eligible_indices, stage_results, strict=True):
            results[index] = result
    if any(result is None for result in results):
        raise RuntimeError("batch stage barrier left an item without a result")
    return tuple(result for result in results if result is not None)

def run_production(
    request: Any,
    output_root: str | Path,
    adapters: WorkflowAdapters,
    *,
    clock: Callable[[], datetime] | None = None,
    stage: int | str = 4,
) -> ProductionResult | BatchProductionResult:
    """Run one Production Item or batch through the requested resumable major stage."""

    rules = _load_json(RULES_PATH)
    stage_contract = rules["majorStageContract"]
    normalized_selector = stage_contract["aliases"].get(str(stage).strip().lower())
    target_stage = next(
        (
            item["number"]
            for item in stage_contract["stages"]
            if item["selector"] == normalized_selector
        ),
        None,
    )
    if target_stage is None:
        return ProductionResult(
            "needs_input",
            "invalid-production-item",
            rules["resultStates"]["needs_input"],
            Path(output_root).resolve(),
            error_code=rules["errorCodes"]["invalidProductionRequest"],
            message="大阶段必须是 1、2、3、4 或对应的 replacement、image、data、final。",
        )
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
            request,
            output_root,
            adapters,
            clock=clock,
            target_stage=target_stage,
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
        results = _run_batch_with_stage_barriers(
            raw_items,
            lambda item, stage_number: _run_batch_item(
                item,
                output_root,
                adapters,
                clock=clock,
                target_stage=stage_number,
            ),
            rules,
            target_stage,
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
    def run_resolved_item(
        item: dict[str, Any], stage_number: int
    ) -> ProductionResult:
        item_id = item["productionItemId"]
        if item_id not in scope:
            return _run_batch_item(
                item,
                output_root,
                adapters,
                clock=clock,
                target_stage=stage_number,
            )
        if item_id in preparation_failures:
            failed_request = effective_requests.get(item_id, item)
            return _run_batch_item(
                failed_request,
                output_root,
                adapters,
                clock=clock,
                preparation_stop=preparation_failures[item_id],
                target_stage=stage_number,
            )
        if item_id not in effective_requests or item_id not in resolutions:
            return _run_batch_item(
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
                target_stage=stage_number,
            )
        return _run_batch_item(
            effective_requests[item_id],
            output_root,
            adapters,
            clock=clock,
            prepared_source_analysis=analyses.get(item_id),
            shared_policy_resolution=resolutions[item_id],
            target_stage=stage_number,
        )
    item_results = _run_batch_with_stage_barriers(
        raw_items,
        run_resolved_item,
        rules,
        target_stage,
    )
    return BatchProductionResult(
        batch_id=batch_id,
        items=item_results,
        shared_policy_applied=True,
    )
