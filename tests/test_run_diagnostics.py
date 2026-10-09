from __future__ import annotations

import json

from lumen.context import ModelInputManifest, ProviderRequestReceipt
from lumen.events import (
    ContextCompactionCompleted,
    TimelineEventRecord,
    ToolCallFinished,
    UsageUpdated,
)
from lumen.run_diagnostics import build_run_diagnostic
from lumen.sessions import TurnRecord


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _manifest(step: int, *, tool_digest: str) -> ModelInputManifest:
    return ModelInputManifest(
        session_id="session-one",
        step=step,
        route="test:model",
        provider="test",
        model="model",
        context_fingerprint=_digest("1"),
        message_count=step,
        tool_count=1,
        instructions_digest=_digest("2"),
        message_history_digest=_digest(str(step)),
        tool_schema_digest=tool_digest,
        context_sources_digest=_digest("3"),
        stable_prefix_digest=_digest("4"),
        dynamic_tail_digest=_digest(str(step + 4)),
        request_fingerprint=_digest(str(step + 6)),
    )


def _receipt(step: int, manifest: ModelInputManifest) -> ProviderRequestReceipt:
    return ProviderRequestReceipt(
        step=step,
        route="test:model",
        provider="test",
        model="model",
        instructions_tokens=10,
        messages_tokens=40,
        tools_tokens=20,
        output_reserve_tokens=20,
        total_tokens=90,
        hard_limit_tokens=100,
        visible_tools=("run_command",),
        visible_tool_digest=manifest.tool_schema_digest,
        context_fingerprint=manifest.context_fingerprint,
        input_manifest=manifest,
    )


def test_run_diagnostic_is_bounded_and_omits_sensitive_content() -> None:
    first = _manifest(1, tool_digest=_digest("a"))
    second = _manifest(2, tool_digest=_digest("b"))
    events = [
        TimelineEventRecord.from_event(
            ToolCallFinished("call-secret", "run_command", "SECRET_OUTPUT", False, 6.0),
            sequence=1,
        ),
        TimelineEventRecord.from_event(
            ContextCompactionCompleted(active_message_count=4, summary_tokens_estimate=20),
            sequence=2,
        ),
        TimelineEventRecord.from_event(
            UsageUpdated(
                usage={"input_tokens": 100, "output_tokens": 20},
                request_count=2,
                tool_call_count=1,
                context_tokens_estimate=80,
                elapsed_seconds=10.0,
            ),
            sequence=3,
        ),
    ]
    turn = TurnRecord(
        user_input="SECRET_PROMPT",
        messages=[],
        approvals=[],
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 0,
            "cost": "0.0125",
            "api_key": "SECRET_KEY",
        },
        status="failed",
        created_at="2026-09-02T00:00:00Z",
        diagnostics=[
            {
                "name": "run_command",
                "error_category": "timeout",
                "message": "SECRET_ERROR",
            }
        ],
        error_message="SECRET_FAILURE",
        timeline_events=events,
        request_receipts=[_receipt(1, first), _receipt(2, second)],
    )

    diagnostic = build_run_diagnostic(turn)

    assert diagnostic["request_count"] == 2
    assert diagnostic["tool_call_count"] == 1
    assert diagnostic["model_context_host_seconds_estimate"] is None
    assert diagnostic["model_attempt_seconds_sum"] is None
    assert diagnostic["usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cost": "0.0125",
    }
    assert diagnostic["schema_changes"] == [
        {"step": 2, "changed": ["messages", "tools", "dynamic_tail"]}
    ]
    assert diagnostic["compaction"] == {"successes": 1, "failures": 0}
    assert diagnostic["error_categories"] == ["timeout"]
    assert diagnostic["bottlenecks"] == [
        "run_failed",
        "tool_time_dominant",
        "context_near_hard_limit",
        "request_prefix_changed",
        "cache_reuse_not_observed",
    ]
    serialized = json.dumps(diagnostic)
    for secret in ("SECRET_PROMPT", "SECRET_OUTPUT", "SECRET_KEY", "SECRET_ERROR", "SECRET_FAILURE"):
        assert secret not in serialized


def test_running_turn_is_projected_as_interrupted() -> None:
    turn = TurnRecord(
        user_input="continue",
        messages=[],
        approvals=[],
        usage={},
        status="running",
        created_at="2026-09-02T00:00:00Z",
    )

    assert build_run_diagnostic(turn)["status"] == "interrupted"


def test_parallel_timing_retries_and_missing_cache_are_not_inferred() -> None:
    turn = TurnRecord(
        user_input="check", messages=[], approvals=[], usage={}, status="completed", created_at="now",
        diagnostics=[
            {"kind": "request_observed", "request_index": index, "elapsed_seconds": elapsed}
            for index, elapsed in [(1, 1.0), (1, 2.0), (2, 0.5)]
        ] + [{"kind": "model_retry", "delay_seconds": 2.0}],
        timeline_events=[TimelineEventRecord.from_event(
            ToolCallFinished(str(index), "search_text", "", False, 6.0), sequence=index,
        ) for index in (1, 2)] + [TimelineEventRecord.from_event(
            UsageUpdated(usage={}, request_count=2, tool_call_count=2,
                         context_tokens_estimate=0, elapsed_seconds=10.0), sequence=3,
        )],
    )
    diagnostic = build_run_diagnostic(turn)
    assert diagnostic["schema_version"] == 2
    assert diagnostic["tool_elapsed_seconds_sum"] == 12.0
    assert diagnostic["tool_wall_seconds"] is None
    assert diagnostic["approval_wall_seconds"] is None
    assert diagnostic["model_context_host_seconds_estimate"] is None
    assert diagnostic["model_attempt_seconds_sum"] == 3.5
    assert diagnostic["model_attempt_count"] == 3
    assert diagnostic["model_retry_count"] == 1
    assert diagnostic["retry_scheduled_seconds_sum"] == 2.0
    assert "cache_reuse_not_observed" not in diagnostic["bottlenecks"]
    turn.diagnostics.append({"execution_seconds": None})
    assert build_run_diagnostic(turn)["tool_execution_seconds_sum"] is None
