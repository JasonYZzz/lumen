"""Tests for the ``/context`` TUI command rendering (M2)."""

from __future__ import annotations

from lumen.context import ContextControlResult
from lumen.ui.app import LumenApp


def _result(**payload: object) -> ContextControlResult:
    return ContextControlResult(
        status="ok",
        message="71240 / 200000 tokens",
        payload=dict(payload),  # type: ignore[arg-type]
    )


def test_format_context_renders_zone_table_and_pressure() -> None:
    result = _result(
        zones=[
            {"zone": "system", "tokens": 8210, "share": 0.064, "survival": "pinned"},
            {"zone": "recent_history", "tokens": 30440, "share": 0.238, "survival": "pinned"},
        ],
        pressure=[
            {"label": "MCP schemas", "tokens": 7200, "source": "mcp:filesystem"},
        ],
        capabilities=[
            {
                "name": "filesystem_search",
                "origin": "mcp:filesystem",
                "deferred": True,
                "loaded": False,
                "tokens": 21,
            }
        ],
        skill_working_set=[{"name": "review", "tokens": 48}],
        estimated=False,
    )
    text = LumenApp.format_context_result(result)
    assert "71240 / 200000 tokens" in text
    assert "system" in text and "recent_history" in text
    assert "Top pressure" in text
    assert "MCP schemas: 7200" in text
    assert "filesystem_search: 21 tokens  [deferred]" in text
    assert "review: 48 tokens" in text
    # Not estimated -> no estimation disclaimer.
    assert "estimated" not in text.lower() or "no known profile" not in text


def test_format_context_marks_estimated_window() -> None:
    result = _result(
        zones=[{"zone": "system", "tokens": 100, "share": 0.0, "survival": "pinned"}],
        pressure=[],
        estimated=True,
    )
    text = LumenApp.format_context_result(result)
    assert "estimated" in text.lower()


def test_format_context_empty_payload_uses_message() -> None:
    result = ContextControlResult(status="ok", message="no context prepared yet", payload={})
    text = LumenApp.format_context_result(result)
    assert text == "no context prepared yet"


def test_format_context_renders_latest_run_diagnostic() -> None:
    result = _result(
        latest_run={
            "status": "failed",
            "request_count": 2,
            "tool_call_count": 1,
            "elapsed_seconds": 10.0,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0},
            "bottlenecks": ["tool_time_dominant"],
            "slow_tools": [{"name": "run_command", "elapsed_seconds": 6.0, "status": "ok"}],
            "schema_changes": [{"step": 2, "changed": ["tools"]}],
            "model_context_host_seconds_estimate": 4.0,
        }
    )

    text = LumenApp.format_context_result(result)

    assert "Latest run: status=failed  requests=2  tools=1  elapsed=10.00s" in text
    assert "Signals: tool_time_dominant" in text
    assert "Tool: run_command  6.00s" in text
    assert "Request step 2 changed: tools" in text
    assert "Non-tool time estimate: 4.00s" in text
