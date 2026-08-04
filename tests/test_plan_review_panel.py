from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult

from lumen.ui.plan_review_panel import PlanReviewPanel


class PlanReviewHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield PlanReviewPanel()

    @on(PlanReviewPanel.Decision)
    def handle_decision(self, event: PlanReviewPanel.Decision) -> None:
        self.decisions.append(event.mode)
        self.query_one(PlanReviewPanel).hide()


async def test_plan_review_maps_numbered_options_to_execution_modes() -> None:
    app = PlanReviewHost()
    async with app.run_test(size=(90, 18)) as pilot:
        panel = app.query_one(PlanReviewPanel)
        panel.show(step_count=3)
        await pilot.pause()

        assert panel.has_class("visible")
        await pilot.press("2")
        await pilot.pause()

        assert app.decisions == ["accept_edits"]
        assert not panel.has_class("visible")


async def test_plan_review_escape_keeps_planning() -> None:
    app = PlanReviewHost()
    async with app.run_test() as pilot:
        panel = app.query_one(PlanReviewPanel)
        panel.show(step_count=0)
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert app.decisions == [None]
