from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class WebModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class AttachmentBody(WebModel):
    artifact_ref: str = Field(alias="artifactRef", pattern=r"^sha256:[0-9a-f]{64}$")
    kind: Literal["image"] = "image"
    media_type: str = Field(alias="mediaType", min_length=1, max_length=100)
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(alias="byteSize", ge=1, le=20 * 1024 * 1024)


class StartRunBody(WebModel):
    input: str = Field(min_length=1)
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=200)
    attachments: list[AttachmentBody] = Field(default_factory=list[AttachmentBody], max_length=8)


class RetryRunBody(WebModel):
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=200)


class StartLiveBody(WebModel):
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=200)
    sdp: str | None = Field(default=None, min_length=1, max_length=128_000)
    route: str | None = Field(default=None, min_length=1, max_length=100)


class QueueInputBody(WebModel):
    text: str = Field(min_length=1)
    mode: Literal["steer", "follow_up"] = "steer"
    attachments: list[AttachmentBody] = Field(default_factory=list[AttachmentBody], max_length=8)


class ApprovalBody(WebModel):
    approved: bool
    scope: Literal["once", "session", "always"] = "once"


class WorkspaceSettingsBody(WebModel):
    model: str | None = None


class ModelConfigurationBody(WebModel):
    expected_revision: str = Field(alias="expectedRevision", min_length=8, max_length=100)
    id: str = Field(min_length=1, max_length=300)
    api: Literal[
        "chat",
        "responses",
        "openai-completions",
        "openai-responses",
        "chat-completions",
    ] | None = None
    base_url: str | None = Field(default=None, alias="baseUrl", max_length=2000)
    api_key_env: str | None = Field(
        default=None,
        alias="apiKeyEnv",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    settings: dict[str, Any] = Field(default_factory=dict[str, Any])
    context: dict[str, Any] = Field(default_factory=dict[str, Any])
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"], alias="inputModalities"
    )
    set_default: bool = Field(default=False, alias="setDefault")


class ConfigurationRevisionBody(WebModel):
    expected_revision: str = Field(alias="expectedRevision", min_length=8, max_length=100)


class SessionSettingsBody(WebModel):
    approval_mode: Literal["manual", "accept_edits", "auto"] | None = Field(
        default=None, alias="approvalMode"
    )
    collaboration_mode: Literal["default", "plan"] | None = Field(default=None, alias="collaborationMode")
    transcript_density: Literal["normal", "verbose"] | None = Field(default=None, alias="transcriptDensity")


class RenameSessionBody(WebModel):
    title: str = Field(min_length=1, max_length=80)


class ForkSessionBody(WebModel):
    through_turn: int = Field(alias="throughTurn", ge=0)


class PlanReviewBody(WebModel):
    action: Literal["approve", "reject"]
    revision: int = Field(ge=1)
    client_request_id: str = Field(alias="clientRequestId", min_length=1, max_length=200)
    feedback: str = ""


class ChildRunActionBody(WebModel):
    action: Literal["cancel", "approve_import", "reject_import", "close"]


class AgentActionBody(WebModel):
    action: Literal[
        "send_message",
        "continue",
        "interrupt",
        "approve_import",
        "reject_import",
        "close",
    ]
    message: str | None = None
    task: str | None = None
    resolution: str | None = None
    reason: str | None = None


class VerificationWaiverBody(WebModel):
    scope: list[str] = Field(default_factory=list[str])
    reason: str = Field(min_length=1, max_length=1000)


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
