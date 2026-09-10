"""Session-scoped collaboration and approval settings."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lumen.reasoning import ReasoningSelection


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
    reasoning: dict[str, ReasoningSelection] = {}


PLAN_MODE_INSTRUCTIONS = """以只读方式工作。调查问题并提出实施计划。
不要编辑文件、运行会改变状态的命令或影响外部系统。最终回复前调用 set_plan,
给出简洁目标和实施步骤。所有未来步骤保持 pending; Host 会请用户审查准确的计划版本后再执行。
"""


DEFAULT_MODE_INSTRUCTIONS = """正常执行用户请求。多步骤工作应保持结构化计划为最新状态;
工具副作用仍由独立审批策略控制。
"""


def apply_collaboration_context(prompt: str, mode: CollaborationMode) -> str:
    guidance = PLAN_MODE_INSTRUCTIONS if mode is CollaborationMode.PLAN else DEFAULT_MODE_INSTRUCTIONS
    return (
        f'<collaboration-mode name="{mode.value}">\n{guidance}'
        "本轮模式说明覆盖先前轮次中的协作模式说明。\n"
        "</collaboration-mode>\n\n"
        f"{prompt}"
    )
