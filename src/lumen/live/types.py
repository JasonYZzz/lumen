"""Durable and transport-neutral contracts for realtime voice sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lumen.live.protocol import LiveBrowserHandshake


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LiveConnectionState(StrEnum):
    CREATING = "creating"
    CONNECTING = "connecting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class LiveActivityState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    PROCESSING = "processing"
    APPROVAL_PENDING = "approval_pending"
    TOOL_RUNNING = "tool_running"
    ASSISTANT_SPEAKING = "assistant_speaking"
    INTERRUPTED = "interrupted"


class LiveEventKind(StrEnum):
    SESSION_CREATED = "live.session.created"
    SESSION_CONNECTED = "live.session.connected"
    SESSION_RECONNECTING = "live.session.reconnecting"
    SESSION_ENDED = "live.session.ended"
    SESSION_FAILED = "live.session.failed"
    TURN_STARTED = "live.turn.started"
    SPEECH_STARTED = "live.input.speech_started"
    SPEECH_STOPPED = "live.input.speech_stopped"
    INPUT_TRANSCRIPT_DELTA = "live.input.transcript.delta"
    INPUT_TRANSCRIPT_COMPLETED = "live.input.transcript.completed"
    RESPONSE_STARTED = "live.response.started"
    RESPONSE_AUDIO_STARTED = "live.response.audio_started"
    RESPONSE_TRANSCRIPT_DELTA = "live.response.transcript.delta"
    RESPONSE_COMPLETED = "live.response.completed"
    RESPONSE_APPROVED = "live.response.approved"
    COMPLETION_BLOCKED = "live.completion.blocked"
    RESPONSE_INTERRUPTED = "live.response.interrupted"
    TOOL_STARTED = "live.tool.started"
    TOOL_APPROVAL_PENDING = "live.tool.approval_pending"
    TOOL_FINISHED = "live.tool.finished"
    USAGE_UPDATED = "live.usage.updated"
    RECONCILIATION_REQUIRED = "live.reconciliation_required"


class LiveSessionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    session_id: str
    provider: str = "openai"


class LiveUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    details: dict[str, Any] = Field(default_factory=dict[str, Any])


class LiveSessionState(BaseModel):
    """Materialized projection persisted by append-only v9 records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: LiveSessionRef
    connection: LiveConnectionState = LiveConnectionState.CREATING
    activity: LiveActivityState = LiveActivityState.IDLE
    model: str
    voice: str
    route: str | None = None
    region: str | None = None
    media_kind: str | None = None
    completion_control: str | None = None
    turn_count: int = Field(default=0, ge=0)
    usage: LiveUsage = Field(default_factory=LiveUsage)
    last_user_transcript: str | None = None
    last_assistant_transcript: str | None = None
    pending_call_ids: tuple[str, ...] = ()
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class SessionLiveState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sessions: tuple[LiveSessionState, ...] = ()

    def get(self, live_session_id: str) -> LiveSessionState | None:
        return next((item for item in self.sessions if item.ref.id == live_session_id), None)

    def upsert(self, state: LiveSessionState) -> SessionLiveState:
        items = [item for item in self.sessions if item.ref.id != state.ref.id]
        items.append(state)
        items.sort(key=lambda item: (item.created_at, item.ref.id))
        return self.model_copy(update={"sessions": tuple(items)})


class LiveEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    live_session_id: str
    session_id: str
    kind: LiveEventKind
    data: dict[str, Any] = Field(default_factory=dict[str, Any])
    created_at: str = Field(default_factory=utc_now)


class LiveConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    client_request_id: str = Field(min_length=1, max_length=200)
    sdp: str | None = Field(default=None, min_length=1, max_length=128_000)
    route: str | None = Field(default=None, min_length=1, max_length=100)


class LiveConnectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    live_session_id: str
    handshake: LiveBrowserHandshake
    answer_sdp: str | None = None
    state: LiveSessionState


class LiveActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: LiveSessionState


__all__ = [
    "LiveActionResult",
    "LiveActivityState",
    "LiveConnectRequest",
    "LiveConnectResult",
    "LiveConnectionState",
    "LiveEvent",
    "LiveEventKind",
    "LiveSessionRef",
    "LiveSessionState",
    "LiveUsage",
    "SessionLiveState",
    "utc_now",
]
