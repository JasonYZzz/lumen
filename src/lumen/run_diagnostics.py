"""Bounded, content-free projections over durable run facts."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from lumen.events import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ToolCallFinished,
    UsageUpdated,
)
from lumen.sessions import TurnRecord

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost",
)
_MANIFEST_DIGESTS = {
    "instructions": "instructions_digest",
    "messages": "message_history_digest",
    "tools": "tool_schema_digest",
    "context_sources": "context_sources_digest",
    "stable_prefix": "stable_prefix_digest",
    "dynamic_tail": "dynamic_tail_digest",
}


def build_run_diagnostic(turn: TurnRecord) -> dict[str, Any]:
    """Project one turn without copying prompts, outputs, arguments, or errors."""

    latest_usage: UsageUpdated | None = None
    tool_events: list[ToolCallFinished] = []
    compaction_successes = 0
    compaction_failures = 0
    for record in turn.timeline_events:
        event = record.to_event()
        if isinstance(event, UsageUpdated):
            latest_usage = event
        elif isinstance(event, ToolCallFinished):
            tool_events.append(event)
        elif isinstance(event, ContextCompactionCompleted):
            compaction_successes += 1
        elif isinstance(event, ContextCompactionFailed):
            compaction_failures += 1

    request_count = (
        latest_usage.request_count if latest_usage is not None else len(turn.request_receipts)
    )
    tool_call_count = latest_usage.tool_call_count if latest_usage is not None else len(tool_events)
    elapsed_seconds = latest_usage.elapsed_seconds if latest_usage is not None else None
    tool_elapsed = sum(max(0.0, event.elapsed_seconds) for event in tool_events)
    attempts = [item for item in turn.diagnostics if item.get("kind") == "request_observed"]
    retries = [item for item in turn.diagnostics if item.get("kind") == "model_retry"]
    executions = [item for item in turn.diagnostics if "execution_seconds" in item]

    receipts = turn.request_receipts
    routes = list(dict.fromkeys(receipt.route for receipt in receipts))[:8]
    estimated_request_tokens = sum(receipt.total_tokens for receipt in receipts)
    schema_changes = _schema_changes(turn)
    bottlenecks: list[str] = []
    if turn.status == "failed":
        bottlenecks.append("run_failed")
    if elapsed_seconds and tool_events and max(event.elapsed_seconds for event in tool_events) >= (
        elapsed_seconds * 0.5
    ):
        bottlenecks.append("tool_time_dominant")
    if any(receipt.total_tokens >= receipt.hard_limit_tokens * 0.8 for receipt in receipts):
        bottlenecks.append("context_near_hard_limit")
    if schema_changes:
        bottlenecks.append("request_prefix_changed")
    usage = _safe_usage(turn.usage)
    if request_count > 1 and usage.get("cache_read_tokens") in (0, 0.0, "0"):
        bottlenecks.append("cache_reuse_not_observed")

    slow_tools = sorted(tool_events, key=lambda event: event.elapsed_seconds, reverse=True)[:5]
    error_categories = list(
        dict.fromkeys(
            str(item.get("error_category") or item.get("category"))[:64]
            for item in turn.diagnostics
            if item.get("error_category") or item.get("category")
        )
    )[:5]
    return {
        "schema_version": 2,
        "created_at": turn.created_at,
        "status": "interrupted" if turn.status == "running" else turn.status,
        "request_count": request_count,
        "tool_call_count": tool_call_count,
        "elapsed_seconds": elapsed_seconds,
        "tool_elapsed_seconds_sum": tool_elapsed,
        # Retain the legacy key, but overlapping tool intervals cannot be
        # subtracted from run wall time to infer model/context/Host time.
        "model_context_host_seconds_estimate": None,
        "timing_estimated": False,
        "model_attempt_count": len(attempts) if attempts else None,
        "model_retry_count": (
            len(attempts) - len({item.get("request_index") for item in attempts})
            if attempts else None
        ),
        "model_attempt_seconds_sum": _seconds_sum(attempts, "elapsed_seconds"),
        "retry_scheduled_seconds_sum": _seconds_sum(retries, "delay_seconds"),
        "tool_execution_seconds_sum": _seconds_sum(executions, "execution_seconds"),
        "tool_wall_seconds": None,
        "approval_wall_seconds": None,
        "context_tokens_estimate": (
            latest_usage.context_tokens_estimate if latest_usage is not None else None
        ),
        "estimated_request_tokens_sum": estimated_request_tokens,
        "usage": usage,
        "routes": routes,
        "compaction": {
            "successes": compaction_successes,
            "failures": compaction_failures,
        },
        "slow_tools": [
            {
                "name": event.name[:128],
                "status": "error" if event.is_error else "ok",
                "elapsed_seconds": max(0.0, event.elapsed_seconds),
            }
            for event in slow_tools
        ],
        "schema_changes": schema_changes,
        "error_categories": error_categories,
        "bottlenecks": bottlenecks,
        "redaction": "content_omitted",
    }


def _seconds_sum(items: list[dict[str, Any]], key: str) -> float | None:
    if not items:
        return None
    total = 0.0
    for item in items:
        value = item.get(key)
        # A replay has no execution duration; incomplete observations stay
        # unknown rather than making missing measurements look like zero.
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        total += value
    return total


def _schema_changes(turn: TurnRecord) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    previous: object | None = None
    for receipt in turn.request_receipts:
        manifest = receipt.input_manifest
        if manifest is None:
            previous = None
            continue
        if previous is not None:
            changed = [
                label
                for label, field_name in _MANIFEST_DIGESTS.items()
                if getattr(previous, field_name) != getattr(manifest, field_name)
            ]
            if changed:
                changes.append({"step": receipt.step, "changed": changed})
                if len(changes) == 10:
                    break
        previous = manifest
    return changes


def _safe_usage(usage: dict[str, Any]) -> dict[str, int | float | str]:
    safe: dict[str, int | float | str] = {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            safe[key] = max(0, value)
        elif isinstance(value, float) and math.isfinite(value):
            safe[key] = max(0.0, value)
        elif isinstance(value, (str, Decimal)):
            try:
                decimal = Decimal(value)
            except InvalidOperation:
                continue
            if decimal.is_finite() and decimal >= 0:
                safe[key] = format(decimal, "f")
    return safe


__all__ = ["build_run_diagnostic"]
