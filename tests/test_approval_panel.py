from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Static

from lumen.events import ApprovalRequest, ToolApprovalBatchPending, ToolApprovalPending
from lumen.ui.approval_panel import ApprovalPanel


def _request(call_id: str, name: str = "write_file") -> ToolApprovalPending:
    return ToolApprovalPending(
        call_id=call_id,
        name=name,
        args={"path": f"outputs/{call_id}.md"},
        origin="builtin",
        risk="write",
    )


class ApprovalHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[tuple[str, bool]] = []
        self.remembered: list[str] = []
        self.batch_decisions: list[tuple[tuple[str, ...], bool]] = []

    def compose(self) -> ComposeResult:
        yield ApprovalPanel()

    @on(ApprovalPanel.Decision)
    def handle_decision(self, event: ApprovalPanel.Decision) -> None:
        self.decisions.append((event.call_id, event.approved))
        if event.remember:
            self.remembered.append(event.call_id)
        self.query_one(ApprovalPanel).resolve(event.call_id)

    @on(ApprovalPanel.BatchDecision)
    def handle_batch_decision(self, event: ApprovalPanel.BatchDecision) -> None:
        self.batch_decisions.append((event.call_ids, event.approved))
        self.query_one(ApprovalPanel).clear()


async def test_approval_panel_advances_numbered_vertical_queue() -> None:
    app = ApprovalHost()
    async with app.run_test(size=(80, 20)) as pilot:
        panel = app.query_one(ApprovalPanel)
        panel.enqueue(_request("first"))
        panel.enqueue(_request("second", "run_command"))
        await pilot.pause()

        assert panel.pending_count == 2
        assert panel.active_request is not None and panel.active_request.call_id == "first"
        await pilot.press("enter")
        await pilot.pause()
        assert app.decisions == [("first", True)]
        assert panel.active_request is not None and panel.active_request.call_id == "second"

        await pilot.press("3")
        await pilot.pause()
        assert app.decisions == [("first", True), ("second", False)]
        assert panel.pending_count == 0
        assert not panel.has_class("visible")


async def test_approval_panel_can_remember_capability_for_session() -> None:
    app = ApprovalHost()
    async with app.run_test(size=(80, 20)) as pilot:
        panel = app.query_one(ApprovalPanel)
        panel.enqueue(_request("remember-me"))
        await pilot.pause()

        await pilot.press("2")
        await pilot.pause()

        assert app.decisions == [("remember-me", True)]
        assert app.remembered == ["remember-me"]


async def test_approval_panel_summarizes_target_path() -> None:
    app = ApprovalHost()
    async with app.run_test() as pilot:
        panel = app.query_one(ApprovalPanel)
        panel.enqueue(_request("report"))
        await pilot.pause()

        assert "outputs/report.md" in str(panel._args.content)  # type: ignore[reportPrivateUsage]


async def test_approval_panel_colors_edit_file_diff() -> None:
    """edit_file previews render as colored diff Text, not a raw dump."""

    app = ApprovalHost()
    async with app.run_test() as pilot:
        panel = app.query_one(ApprovalPanel)
        panel.enqueue(
            ToolApprovalPending(
                call_id="edit-1",
                name="edit_file",
                args={"path": "a.py", "find": "old line", "replace": "new line"},
                origin="builtin",
                risk="write",
            )
        )
        await pilot.pause()

        content = panel._args.content  # type: ignore[reportPrivateUsage]
        assert isinstance(content, Text)
        assert "-old line" in content.plain
        assert "+new line" in content.plain
        # The +/- lines carry colored spans distinct from the dim context.
        span_styles = {str(span.style) for span in content.spans}
        assert "dim" in span_styles
        assert len(span_styles) >= 3


async def test_batch_approval_uses_numbered_claude_style_choices() -> None:
    app = ApprovalHost()
    batch = ToolApprovalBatchPending(
        batch_id="batch-1",
        requests=(
            ApprovalRequest("one", "write_file", {"path": "one"}, "builtin", "write"),
            ApprovalRequest("two", "run_command", {"argv": ["pytest"]}, "builtin", "execute"),
        ),
        risk_summary="1xexecute, 1xwrite",
    )
    async with app.run_test() as pilot:
        panel = app.query_one(ApprovalPanel)
        panel.enqueue_batch(batch)
        await pilot.pause()

        options = "\n".join(
            str(widget.content) for widget in panel.query(".approval-option").results(Static)
        )
        assert "1. Allow all" in options
        assert "2. Review individually" in options
        assert "3. Deny all" in options

        await pilot.press("3")
        await pilot.pause()
        assert app.batch_decisions == [(('one', 'two'), False)]
