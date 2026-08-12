"""Provider-neutral contracts for realtime voice model and media adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LiveMediaKind(StrEnum):
    DIRECT_WEBRTC = "direct_webrtc"
    HOST_WEBSOCKET = "host_websocket"
    MANAGED_RTC = "managed_rtc"


class LiveCompletionControl(StrEnum):
    NATIVE_REQUIRED_TOOL = "native_required_tool"
    HOST_GATED_SYNTHESIS = "host_gated_synthesis"
    ADVISORY_ONLY = "advisory_only"


class LiveProviderEventKind(StrEnum):
    TRANSPORT_ERROR = "transport_error"
    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    INPUT_TRANSCRIPT_DELTA = "input_transcript_delta"
    INPUT_TRANSCRIPT_COMPLETED = "input_transcript_completed"
    RESPONSE_STARTED = "response_started"
    RESPONSE_AUDIO_DELTA = "response_audio_delta"
    RESPONSE_TRANSCRIPT_DELTA = "response_transcript_delta"
    TOOL_CALL_READY = "tool_call_ready"
    RESPONSE_COMPLETED = "response_completed"


class LiveProviderCommandKind(StrEnum):
    CANCEL_RESPONSE = "cancel_response"
    SUBMIT_TOOL_RESULT = "submit_tool_result"
    CONTINUE_RESPONSE = "continue_response"
    SPEAK_APPROVED = "speak_approved"


class LiveProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media: frozenset[LiveMediaKind]
    function_calling: bool
    interruption: bool
    input_transcription: bool
    server_tool_authority: bool
    completion_control: LiveCompletionControl
    max_session_seconds: int = Field(ge=60)


class LiveSessionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: str
    tools: tuple[dict[str, Any], ...] = ()
    voice: str | None = None
    turn_detection: dict[str, Any] = Field(default_factory=dict[str, Any])
    input_transcription: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    strict_completion: bool = True


class LiveProviderOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    live_session_id: str
    session_id: str
    spec: LiveSessionSpec
    browser_offer_sdp: str | None = None


class LiveBrowserHandshake(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LiveMediaKind
    answer_sdp: str | None = None
    media_path: str | None = None
    input_sample_rate: int | None = Field(default=None, ge=8_000, le=96_000)
    output_sample_rate: int | None = Field(default=None, ge=8_000, le=96_000)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> Self:
        if self.kind is LiveMediaKind.DIRECT_WEBRTC and not self.answer_sdp:
            raise ValueError("direct WebRTC handshake requires answer_sdp")
        if self.kind is LiveMediaKind.HOST_WEBSOCKET and not self.media_path:
            raise ValueError("Host WebSocket handshake requires media_path")
        return self


class LiveProviderEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LiveProviderEventKind
    event_id: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None
    delta: str | None = None
    transcript: str | None = None
    response_id: str | None = None
    response_status: str | None = None
    audio: bytes | None = None
    usage: dict[str, Any] | None = None
    message: str | None = None


class LiveProviderCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: LiveProviderCommandKind
    call_id: str | None = None
    output: dict[str, Any] | None = None
    required_tool: bool | None = None
    instructions: str | None = None
    answer: str | None = None

    @classmethod
    def cancel_response(cls) -> LiveProviderCommand:
        return cls(kind=LiveProviderCommandKind.CANCEL_RESPONSE)

    @classmethod
    def submit_tool_result(cls, call_id: str, output: dict[str, Any]) -> LiveProviderCommand:
        return cls(
            kind=LiveProviderCommandKind.SUBMIT_TOOL_RESULT,
            call_id=call_id,
            output=output,
        )

    @classmethod
    def continue_response(
        cls,
        *,
        required_tool: bool | None = None,
        instructions: str | None = None,
    ) -> LiveProviderCommand:
        return cls(
            kind=LiveProviderCommandKind.CONTINUE_RESPONSE,
            required_tool=required_tool,
            instructions=instructions,
        )

    @classmethod
    def speak_approved(cls, answer: str) -> LiveProviderCommand:
        return cls(kind=LiveProviderCommandKind.SPEAK_APPROVED, answer=answer)


LiveProviderEventSink = Callable[[LiveProviderEvent], Awaitable[None]]


class LiveProviderConnection(Protocol):
    provider_call_id: str
    handshake: LiveBrowserHandshake

    async def send(self, command: LiveProviderCommand) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def close(self) -> None: ...


class RealtimeProviderAdapter(Protocol):
    provider: str
    capabilities: LiveProviderCapabilities

    async def open(
        self,
        request: LiveProviderOpenRequest,
        event_sink: LiveProviderEventSink,
    ) -> LiveProviderConnection: ...

    async def close(self) -> None: ...


__all__ = [
    "LiveBrowserHandshake",
    "LiveCompletionControl",
    "LiveMediaKind",
    "LiveProviderCapabilities",
    "LiveProviderCommand",
    "LiveProviderCommandKind",
    "LiveProviderConnection",
    "LiveProviderEvent",
    "LiveProviderEventKind",
    "LiveProviderEventSink",
    "LiveProviderOpenRequest",
    "LiveSessionSpec",
    "RealtimeProviderAdapter",
]
