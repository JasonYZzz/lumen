"""Approval mode tests.

Validates the policy that decides whether a tool call mounts the Allow/Deny
panel or short-circuits. Auto approves every explicitly classified risk;
``external_unknown`` still prompts. Ask mode sends every risky tool through
the panel.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Static

from lumen.config import load_config
from lumen.events import ApprovalRequest
from lumen.plan import PlanState
from lumen.resources import ResourceManager
from lumen.ui.app import LumenApp
from lumen.ui.approval_panel import ApprovalPanel


def _make_app(tmp_path: Path, *, default_mode: str = "ask") -> LumenApp:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"""
version: 1
agent:
  name: approval-test
  model:
    id: test
tools:
  builtins: []
permissions:
  default_mode: {default_mode}
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    return LumenApp(config, ResourceManager(config, workspace=tmp_path))


def _request(risk: str, call_id: str = "call-1") -> ApprovalRequest:
    return ApprovalRequest(
        call_id=call_id,
        name=f"tool_{risk}",
        args={},
        origin="builtin",
        risk=risk,
    )


# ---------------------------------------------------------------------------
# Policy: _should_auto_approve
# ---------------------------------------------------------------------------


async def test_manual_mode_allows_reads_but_confirms_risky_actions(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._should_auto_approve("read") is True  # type: ignore[reportPrivateUsage]
        for risk in ("write", "execute", "external"):
            assert app._should_auto_approve(risk) is False  # type: ignore[reportPrivateUsage]


async def test_auto_mode_approves_all_classified_risks(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="auto")
    async with app.run_test() as pilot:
        await pilot.pause()
        # Every classified risk short-circuits. MCP tools whose risk was never
        # declared are external_unknown and must still confirm.
        assert app._should_auto_approve("read") is True  # type: ignore[reportPrivateUsage]
        assert app._should_auto_approve("external") is True  # type: ignore[reportPrivateUsage]
        assert app._should_auto_approve("write") is True  # type: ignore[reportPrivateUsage]
        assert app._should_auto_approve("execute") is True  # type: ignore[reportPrivateUsage]
        assert app._should_auto_approve("external_unknown") is False  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# Short-circuit: _await_inline_approval
# ---------------------------------------------------------------------------


async def test_auto_mode_short_circuits_read_no_card(tmp_path: Path) -> None:
    """In auto mode, a read risk returns immediately without mounting a card."""

    app = _make_app(tmp_path, default_mode="auto")
    async with app.run_test() as pilot:
        await pilot.pause()
        decision = await app._await_inline_approval(_request("read"))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        assert decision.approved is True
        assert "auto-approved" in decision.message
        # No ToolApprovalPending event was rendered: the card should not have
        # been mounted. We assert by checking that no widget has the
        # is-pending class.
        pending_cards = [c for c in app.query("ToolCard").results() if c.has_class("is-pending")]
        assert pending_cards == []


async def test_auto_mode_short_circuits_write_without_card(tmp_path: Path) -> None:
    """Auto mode runs a classified write without mounting an approval card."""

    app = _make_app(tmp_path, default_mode="auto")
    async with app.run_test() as pilot:
        await pilot.pause()
        decision = await app._await_inline_approval(_request("write"))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        assert decision.approved is True
        assert "auto-approved" in decision.message
        pending_cards = [c for c in app.query("ToolCard").results() if c.has_class("is-pending")]
        assert pending_cards == []


async def test_manual_mode_mounts_card_for_risky_action(tmp_path: Path) -> None:
    """In manual mode, a write request mounts the card."""

    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app._await_inline_approval(_request("write")))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        await asyncio.sleep(0)
        await pilot.pause()
        assert not task.done()
        pending_cards = [c for c in app.query("ToolCard").results() if c.has_class("is-pending")]
        assert len(pending_cards) == 1
        app._resolve_all_pending_approvals(approved=False, message="test cleanup")  # type: ignore[reportPrivateUsage]
        await task


async def test_manual_session_rule_skips_repeated_capability_prompt(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        first = asyncio.create_task(app._await_inline_approval(_request("write", "first")))  # type: ignore[reportPrivateUsage]
        await pilot.pause()
        await pilot.press("2")
        await pilot.pause()
        assert (await first).approved

        second = await app._await_inline_approval(_request("write", "second"))  # type: ignore[reportPrivateUsage]
        assert second.approved
        assert "user_session" in second.message
        assert app.query_one(ApprovalPanel).pending_count == 0


# ---------------------------------------------------------------------------
# Mode switching: /mode command + Shift+Tab
# ---------------------------------------------------------------------------


async def test_mode_command_reports_current(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.handle_input("/mode")
        await pilot.pause()
        text = "\n".join(str(w.content) for w in app.query("#messages Static").results(Static))
        assert "manual" in text
        assert "allowing reads; confirming edits" in text


async def test_mode_command_switches_to_auto(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.handle_input("/mode auto")
        await pilot.pause()
        assert app.approval_mode == "auto"
        assert "auto mode on" in str(app.query_one("#status", Static).content)
        # Mode belongs to the composer footer, not the global top bar.
        topbar = str(app.query_one("#topbar", Static).content)
        assert "auto" not in topbar.lower()


async def test_mode_command_rejects_unknown(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.handle_input("/mode yolo")
        await pilot.pause()
        # Mode is unchanged.
        assert app.approval_mode == "manual"
        text = "\n".join(str(w.content) for w in app.query("#messages Static").results(Static))
        assert "Unknown mode" in text


async def test_ctrl_m_does_not_create_a_second_mode_cycle(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="ask")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.approval_mode == "manual"
        await pilot.press("ctrl+m")
        await pilot.pause()
        assert app.approval_mode == "manual"


async def test_plan_mode_blocks_mutations_without_mounting_approval(tmp_path: Path) -> None:
    app = _make_app(tmp_path, default_mode="plan")
    async with app.run_test() as pilot:
        decision = await app._await_inline_approval(  # type: ignore[reportPrivateUsage]
            _request("write")
        )
        await pilot.pause()

        assert decision.approved is False
        assert "blocked in plan mode" in decision.message
        assert list(app.query(".is-pending")) == []


async def test_mode_context_tells_model_when_plan_starts_and_ends(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test():
        app.set_approval_mode("plan")
        plan_prompt = app._apply_permission_mode_context(  # type: ignore[reportPrivateUsage]
            "inspect this"
        )
        assert 'name="plan"' in plan_prompt
        assert "Work read-only" in plan_prompt

        app.set_approval_mode("auto")
        auto_prompt = app._apply_permission_mode_context(  # type: ignore[reportPrivateUsage]
            "implement this"
        )
        assert 'name="auto"' in auto_prompt
        assert "Plan mode is off" in auto_prompt


def test_set_approval_mode_validates(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with pytest.raises(ValueError, match="approval mode"):
        app.set_approval_mode("yolo")


# ---------------------------------------------------------------------------
# Footer removal regression
# ---------------------------------------------------------------------------


async def test_footer_widget_is_gone(tmp_path: Path) -> None:
    """The duplicated Textual Footer at the bottom-right is removed.

    The #status bar (bottom-left) carries the keymap hint; the Footer widget
    was duplicating it. Guard against accidental re-addition.
    """

    from textual.css.query import NoMatches

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with pytest.raises(NoMatches):
            app.query_one("Footer")


# ---------------------------------------------------------------------------
# CommandGate: state-destroying commands blocked during a run
# ---------------------------------------------------------------------------


class _FakeRunningWorker:
    """Stand-in for a Textual Worker mid-run: ``is_running`` is True."""

    is_running = True

    def cancel(self) -> None:  # pragma: no cover - not used in block tests
        pass


def _messages_text(app: LumenApp) -> str:
    return "\n".join(str(w.content) for w in app.query("#messages Static").results(Static))


async def test_read_only_commands_allowed_during_run(tmp_path: Path) -> None:
    """Read-only commands (/help, /mode, /tools) pass the gate during a run."""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/help")
        await pilot.pause()
        text = _messages_text(app)
        assert "/help" in text  # the help line was emitted, not blocked


async def test_new_command_blocked_during_run(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        original_session = app.session
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/new")
        await pilot.pause()
        # Blocked: a hint was emitted and the session was NOT swapped.
        assert "disabled while a run is active" in _messages_text(app)
        assert app.session is original_session


async def test_resume_command_blocked_during_run(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        original_session = app.session
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/resume some-id")
        await pilot.pause()
        assert "disabled while a run is active" in _messages_text(app)
        assert app.session is original_session


async def test_model_switch_blocked_during_run(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/model other")
        await pilot.pause()
        assert "disabled while a run is active" in _messages_text(app)


async def test_model_listing_allowed_during_run(tmp_path: Path) -> None:
    """``/model`` (no arg) is a read-only list and passes the gate."""
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/model")
        await pilot.pause()
        # Not blocked: it lists the model (id "test") instead of refusing.
        assert "disabled while a run is active" not in _messages_text(app)


async def test_retry_command_blocked_during_run(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/retry")
        await pilot.pause()
        assert "can't be used while one is active" in _messages_text(app)


async def test_skill_command_blocked_during_run(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current_worker = _FakeRunningWorker()  # type: ignore[assignment]
        await app.handle_input("/skill:tdd")
        await pilot.pause()
        assert "can't be used while one is active" in _messages_text(app)


# ---------------------------------------------------------------------------
# Session summary isolation (Phase 1.6)
# ---------------------------------------------------------------------------


async def test_new_command_clears_compaction_summary(tmp_path: Path) -> None:
    """/new must NOT carry the previous session's compaction summary into the
    new session — it would seed iterative compaction with stale state."""
    from lumen.context import ContextSummary

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Simulate a prior session having produced a compaction summary.
        app._last_compaction_summary = ContextSummary(goals=["stale goal"])  # type: ignore[reportPrivateUsage]
        await app.handle_input("/new")
        await pilot.pause()
        # The new session starts with no summary — isolation boundary.
        assert app._last_compaction_summary is None  # type: ignore[reportPrivateUsage]


async def test_resume_restores_target_session_summary(tmp_path: Path) -> None:
    """/resume restores the target session's own compaction summary, not a
    stale App-level value."""
    from lumen.context import ContextSummary

    app = _make_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Create a second session that carries a compaction summary.
        other = app.resources.session_repository.create(agent_name="t", model_id="test")
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            SystemPromptPart,
            TextPart,
            UserPromptPart,
        )

        from lumen.context import CompactionRecord

        summary = ContextSummary(goals=["other session goal"])
        record = CompactionRecord(summary, [ModelRequest(parts=[SystemPromptPart(content="s")])], 2, {})
        app.resources.session_repository.append_turn(
            other.id,
            user_input="q",
            messages=[
                ModelRequest(parts=[UserPromptPart(content="q")]),
                ModelResponse(parts=[TextPart(content="a")]),
            ],
            approvals=[],
            usage={},
            status="completed",
            plan=PlanState(),
            diagnostics=[],
            compaction=record,
        )
        # App currently has no summary.
        assert app._last_compaction_summary is None  # type: ignore[reportPrivateUsage]
        await app.handle_input(f"/resume {other.id}")
        await pilot.pause()
        # The resumed session's summary was restored — isolated to it.
        assert app._last_compaction_summary is not None  # type: ignore[reportPrivateUsage]
        assert app._last_compaction_summary.goals == ["other session goal"]  # type: ignore[reportPrivateUsage]
        assert app.session is not None
        assert app.session.id == other.id
