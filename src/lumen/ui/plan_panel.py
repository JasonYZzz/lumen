"""Task panel: renders the agent's structured execution plan as user-facing tasks.

The panel is fed by ``PlanCreated`` and ``PlanUpdated`` events. Each step is
rendered with a single status glyph (✓ ● ○ !) and a state-specific CSS class so
the TUI can theme active, completed, pending, and blocked steps distinctly.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static

from lumen.plan import PlanState, StepStatus

_STATUS_GLYPHS: dict[StepStatus, str] = {
    StepStatus.COMPLETED: "✔",
    StepStatus.IN_PROGRESS: "▣",
    StepStatus.PENDING: "☐",
    StepStatus.BLOCKED: "!",
}

_STATUS_CLASSES: dict[StepStatus, str] = {
    StepStatus.COMPLETED: "plan-step-completed",
    StepStatus.IN_PROGRESS: "plan-step-active",
    StepStatus.PENDING: "plan-step-pending",
    StepStatus.BLOCKED: "plan-step-blocked",
}


class PlanPanel(Vertical):
    """Renders one turn's plan inline with the conversation timeline."""

    DEFAULT_CSS = """
    PlanPanel {
        height: auto;
        background: $background;
        padding: 0 1 1 1;
        margin: 1 1;
        display: none;
    }
    PlanPanel.has-plan { display: block; }
    PlanPanel.plan-collapsed { height: 1; padding-bottom: 0; }
    .plan-header { text-style: bold; color: $text; padding: 0 1; }
    .plan-step { padding: 0 3; }
    .plan-step-active { color: $accent; text-style: bold; }
    .plan-step-completed { color: $text-muted; }
    .plan-step-pending { color: $text-muted; }
    .plan-step-blocked { color: $error; text-style: bold; }
    """

    def __init__(self, plan: PlanState | None = None) -> None:
        super().__init__()
        self._plan = plan
        self._collapsed = False
        self._header = Static("Tasks", classes="plan-header", markup=False)

    def compose(self):  # type: ignore[no-untyped-def]
        yield self._header

    def on_mount(self) -> None:
        self._render_plan()

    def update_plan(self, plan: PlanState) -> None:
        """Replace the plan rendered for this conversation turn."""

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
            completed = sum(step.status is StepStatus.COMPLETED for step in plan.steps)
            if self._collapsed:
                active = next(
                    (step for step in plan.steps if step.status is StepStatus.IN_PROGRESS),
                    next((step for step in plan.steps if step.status is not StepStatus.COMPLETED), None),
                )
                active_text = active.title if active is not None else "Complete"
                self._header.update(f"Tasks {completed}/{len(plan.steps)} · {active_text}")
                return
            self._header.update(f"Tasks  {completed}/{len(plan.steps)}")
            # Collapse state is managed exclusively by collapse(); update_plan
            # only controls has-plan visibility.
            for step in plan.steps:
                glyph = _STATUS_GLYPHS[step.status]
                css = _STATUS_CLASSES[step.status]
                note = f" — {step.note}" if step.note else ""
                self.mount(Static(f"{glyph} {step.title}{note}", classes=f"plan-step {css}"))
        else:
            self._header.update("Tasks")
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
