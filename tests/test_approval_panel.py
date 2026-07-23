from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult

from lumen.events import ToolApprovalPending
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

    def compose(self) -> ComposeResult:
        yield ApprovalPanel()

    @on(ApprovalPanel.Decision)
    def handle_decision(self, event: ApprovalPanel.Decision) -> None:
        self.decisions.append((event.call_id, event.approved))
        self.query_one(ApprovalPanel).resolve(event.call_id)


async def test_approval_panel_advances_vertical_queue_without_default_selection() -> None:
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
        assert app.decisions == []

        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.decisions == [("first", False)]
        assert panel.active_request is not None and panel.active_request.call_id == "second"

        await pilot.press("y", "n")
        await pilot.pause()
        assert app.decisions == [("first", False), ("second", True)]
        assert panel.pending_count == 0
        assert not panel.has_class("visible")


async def test_approval_panel_summarizes_target_path() -> None:
    app = ApprovalHost()
    async with app.run_test() as pilot:
        panel = app.query_one(ApprovalPanel)
        panel.enqueue(_request("report"))
        await pilot.pause()

        assert "outputs/report.md" in str(panel._args.content)  # type: ignore[reportPrivateUsage]
