from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WebModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class StartRunBody(WebModel):
    input: str = Field(min_length=1)
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=200)


class RetryRunBody(WebModel):
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=200)


class QueueInputBody(WebModel):
    text: str = Field(min_length=1)
    mode: Literal["steer", "follow_up"] = "steer"


class ApprovalBody(WebModel):
    approved: bool


class WorkspaceSettingsBody(WebModel):
    model: str | None = None
    approval_mode: Literal["manual", "accept_edits", "plan", "auto"] | None = Field(
        default=None, alias="approvalMode"
    )
    confirmed: bool = False


class ControlBody(WebModel):
    type: Literal[
        "invoke_skill",
        "invoke_prompt",
        "compact_context",
        "memory",
        "set_context_source",
        "cancel_clarification",
    ]
    name: str | None = None
    arguments: str = ""
    action: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict[str, Any])
    client_request_id: str | None = Field(default=None, alias="clientRequestId", min_length=1, max_length=200)
