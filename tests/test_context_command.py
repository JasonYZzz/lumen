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
        estimated=False,
    )
    text = LumenApp._format_context_result(result)
    assert "71240 / 200000 tokens" in text
    assert "system" in text and "recent_history" in text
    assert "Top pressure" in text
    assert "MCP schemas: 7200" in text
    # Not estimated -> no estimation disclaimer.
    assert "estimated" not in text.lower() or "no known profile" not in text


def test_format_context_marks_estimated_window() -> None:
    result = _result(
        zones=[{"zone": "system", "tokens": 100, "share": 0.0, "survival": "pinned"}],
        pressure=[],
        estimated=True,
    )
    text = LumenApp._format_context_result(result)
    assert "estimated" in text.lower()


def test_format_context_empty_payload_uses_message() -> None:
    result = ContextControlResult(status="ok", message="no context prepared yet", payload={})
    text = LumenApp._format_context_result(result)
    assert text == "no context prepared yet"
