"""Todo panel: renders the agent's structured execution plan as user-facing tasks.

The panel is fed by ``PlanCreated`` and ``PlanUpdated`` events. Each step is
rendered with a single status glyph (✓ ● ○ !) and a state-specific CSS class so
the TUI can theme active, completed, pending, and blocked steps distinctly.
"""

from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Static

from lumen.plan import PlanState, StepStatus

_STATUS_GLYPHS: dict[StepStatus, str] = {
    StepStatus.COMPLETED: "✓",
    StepStatus.IN_PROGRESS: "●",
    StepStatus.PENDING: "○",
    StepStatus.BLOCKED: "!",
}

_STATUS_CLASSES: dict[StepStatus, str] = {
    StepStatus.COMPLETED: "plan-step-completed",
    StepStatus.IN_PROGRESS: "plan-step-active",
    StepStatus.PENDING: "plan-step-pending",
    StepStatus.BLOCKED: "plan-step-blocked",
}


class PlanPanel(VerticalScroll):
    """Renders the current plan as a vertical list of step rows."""

    DEFAULT_CSS = """
    PlanPanel {
        /* Fixed-height plan panel pinned at the top of the screen (between
           the topbar and the scrolling message timeline). ``max-height: 8``
           keeps an 8-step plan fully visible; longer plans scroll inside the
           panel rather than pushing the message area down. */
        height: auto;
        max-height: 8;
        border: round $accent 40%;
        background: $surface 50%;
        padding: 0 1;
        margin: 0 2;
        display: none;
    }
    PlanPanel.has-plan { display: block; }
    PlanPanel.plan-collapsed { height: 3; }
    .plan-header { text-style: bold; color: $accent; padding: 0 0 0 1; }
    .plan-step { padding: 0 1; }
    /* Active step: amber + bold — the eye is drawn to what's happening now. */
    .plan-step-active { color: $warning; text-style: bold; }
    /* Completed: green + dimmed to indicate "done, move on". */
    .plan-step-completed { color: $success; text-style: dim; }
    /* Pending: muted — not yet actionable. */
    .plan-step-pending { color: $text-muted; }
    /* Blocked: red + bold — needs attention. */
    .plan-step-blocked { color: $error; text-style: bold; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._plan: PlanState | None = None
        self._collapsed = False
        self._header = Static("Todo", classes="plan-header")

    def compose(self):  # type: ignore[no-untyped-def]
        yield self._header

    def update_plan(self, plan: PlanState) -> None:
        """Replace the rendered plan; auto-collapse if there are no steps."""

        self._plan = plan
        self._render_plan()

    def _render_plan(self) -> None:
        plan = self._plan or PlanState()
        # Drop any previously rendered step rows.
        for child in list(self.children):
            if child is not self._header:
                child.remove()
        if plan.steps:
            self.add_class("has-plan")
            if self._collapsed:
                completed = sum(step.status is StepStatus.COMPLETED for step in plan.steps)
                active = next(
                    (step for step in plan.steps if step.status is StepStatus.IN_PROGRESS),
                    next((step for step in plan.steps if step.status is not StepStatus.COMPLETED), None),
                )
                active_text = active.title if active is not None else "Done"
                self._header.update(f"Todo {completed}/{len(plan.steps)} · active: {active_text}")
                return
            self._header.update("Todo")
            # Collapse state is managed exclusively by collapse(); update_plan
            # only controls has-plan visibility.
            for step in plan.steps:
                glyph = _STATUS_GLYPHS[step.status]
                css = _STATUS_CLASSES[step.status]
                note = f" — {step.note}" if step.note else ""
                self.mount(Static(f"{glyph} {step.title}{note}", classes=f"plan-step {css}"))
        else:
            self._header.update("Todo")
            self.remove_class("has-plan")

    def collapse(self, collapsed: bool) -> None:
        """Toggle between the full plan view and a one-line collapsed view."""

        self._collapsed = collapsed
        if collapsed:
            self.add_class("plan-collapsed")
        else:
            self.remove_class("plan-collapsed")
        self._render_plan()

    def render(self):  # type: ignore[no-untyped-def]
        # The container itself doesn't paint text; children do. We expose a
        # textual summary via ``__str__`` for tests and screen readers.
        return super().render()


def render_plan_summary(plan: PlanState) -> str:
    """Return a single-string rendering of ``plan`` (used by tests and logs)."""

    if not plan.steps:
        return ""
    lines: list[str] = []
    for step in plan.steps:
        glyph = _STATUS_GLYPHS[step.status]
        note = f" — {step.note}" if step.note else ""
        lines.append(f"{glyph} {step.title}{note}")
    return "\n".join(lines)


__all__ = ["PlanPanel", "render_plan_summary"]
