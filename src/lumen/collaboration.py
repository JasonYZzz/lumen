"""Session-scoped collaboration and approval settings."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CollaborationMode(StrEnum):
    DEFAULT = "default"
    PLAN = "plan"


class PlanReviewStatus(StrEnum):
    NONE = "none"
    REVIEW_PENDING = "review_pending"
    APPROVED_WAITING_TO_EXECUTE = "approved_waiting_to_execute"
    EXECUTING = "executing"
    REJECTED = "rejected"


class SessionSettingsState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collaboration_mode: CollaborationMode = CollaborationMode.DEFAULT
    approval_mode: str = "manual"
    plan_review_status: PlanReviewStatus = PlanReviewStatus.NONE
    reviewed_revision: int | None = None
    review_client_request_id: str | None = None
    execution_run_id: str | None = None
    transcript_density: Literal["normal", "verbose"] = "normal"


PLAN_MODE_INSTRUCTIONS = """Work read-only. Explore and produce a proposed implementation plan.
Do not edit files, run mutating commands, or affect external systems. Before the final response,
call set_plan with a concise goal and the proposed implementation steps. Keep every future step
pending; the host will ask the user to review the exact plan revision before execution.
"""


DEFAULT_MODE_INSTRUCTIONS = """Follow the user's request normally. For multi-step work keep the
structured plan current; tool side effects remain governed by the independent approval policy.
"""


def apply_collaboration_context(prompt: str, mode: CollaborationMode) -> str:
    guidance = PLAN_MODE_INSTRUCTIONS if mode is CollaborationMode.PLAN else DEFAULT_MODE_INSTRUCTIONS
    return (
        f'<collaboration-mode name="{mode.value}">\n{guidance}'
        "This current-mode note supersedes collaboration notes from earlier turns.\n"
        "</collaboration-mode>\n\n"
        f"{prompt}"
    )
