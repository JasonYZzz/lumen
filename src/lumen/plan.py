"""Strict plan models and state transitions used by the task controller.

The plan is the public face of agent progress: it is structured, validated, and
shown to the user via the TUI timeline. It never carries private chain-of-thought;
that stays in model-internal reasoning that this layer does not touch.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StepStatus(StrEnum):
    """Lifecycle status of a single plan step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class PlanStepInput(BaseModel):
    """Caller-supplied definition of a step before the controller assigns state."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)


class PlanStep(PlanStepInput):
    """A plan step as the controller stores it: caller input plus current state."""

    status: StepStatus = StepStatus.PENDING
    note: str | None = Field(default=None, max_length=500)


class PlanState(BaseModel):
    """Immutable snapshot of the controller's plan at a point in time."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(default=0, ge=0)
    steps: list[PlanStep] = Field(default_factory=list[PlanStep])

    @model_validator(mode="after")
    def ensure_single_active_step(self) -> PlanState:
        active = [step.id for step in self.steps if step.status is StepStatus.IN_PROGRESS]
        if len(active) > 1:
            raise ValueError(f"plan may contain at most one in_progress step: {active}")
        return self
