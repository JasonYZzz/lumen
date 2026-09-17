"""Model Driver Interface and deterministic replay Implementation.

The Interface is intentionally narrower than an Agent runtime. A driver only
translates one frozen model request into an ordered provider-neutral stream.
It cannot execute tools, request approval, write Session state, compact
context, or decide that a run is complete.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType, TracebackType
from typing import Annotated, Any, Generic, Literal, Protocol, Self, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lumen.context import ModelInputManifest

MessageT = TypeVar("MessageT")
StreamMessageT_co = TypeVar("StreamMessageT_co", covariant=True)


class _DriverContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return MappingProxyType({str(key): _freeze(item) for key, item in mapping.items()})
    if isinstance(value, list | tuple):
        sequence = cast(Sequence[object], value)
        return tuple(_freeze(item) for item in sequence)
    return value


class ModelStopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_CALL = "tool_call"
    LENGTH = "length"
    REFUSAL = "refusal"
    CONTENT_FILTER = "content_filter"
    SUSPENDED = "suspended"
    ERROR = "error"
    UNKNOWN = "unknown"


class ModelNativeTool(_DriverContract):
    """Provider-executed tool included in the frozen request evidence."""

    kind: Literal["web_search"]
    search_context_size: Literal["low", "medium", "high"] = "medium"


class _SequencedEvent(_DriverContract):
    sequence: int = Field(ge=0)


class ModelResponseStarted(_SequencedEvent):
    kind: Literal["response_started"] = "response_started"
    provider_response_id: str | None = None


class ModelTextDelta(_SequencedEvent):
    kind: Literal["text_delta"] = "text_delta"
    content: str


class ModelThinkingDelta(_SequencedEvent):
    kind: Literal["thinking_delta"] = "thinking_delta"
    content: str


class ModelToolCallStarted(_SequencedEvent):
    kind: Literal["tool_call_started"] = "tool_call_started"
    call_id: str
    name: str


class ModelToolArgumentsDelta(_SequencedEvent):
    kind: Literal["tool_arguments_delta"] = "tool_arguments_delta"
    call_id: str
    arguments_delta: str


class ModelToolCallCompleted(_SequencedEvent):
    kind: Literal["tool_call_completed"] = "tool_call_completed"
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict[str, Any])


class ModelUsage(_SequencedEvent):
    kind: Literal["usage"] = "usage"
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    provider_details: dict[str, int | float | str | bool | None] = Field(
        default_factory=dict[str, int | float | str | bool | None]
    )


class ModelResponseCompleted(_SequencedEvent):
    kind: Literal["response_completed"] = "response_completed"
    stop_reason: ModelStopReason
    provider_response_id: str | None = None


class ModelProviderError(_SequencedEvent):
    kind: Literal["provider_error"] = "provider_error"
    category: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = Field(default=None, ge=0)
    replay_safe: bool = True


ModelStreamEvent = Annotated[
    ModelResponseStarted
    | ModelTextDelta
    | ModelThinkingDelta
    | ModelToolCallStarted
    | ModelToolArgumentsDelta
    | ModelToolCallCompleted
    | ModelUsage
    | ModelResponseCompleted
    | ModelProviderError,
    Field(discriminator="kind"),
]


@dataclass(frozen=True, slots=True)
class ModelDriverRequest(Generic[MessageT]):
    """One frozen request passed to a ModelDriver Implementation.

    ``messages`` remains generic so the Driver Seam does not create a second
    canonical message DTO. The production adapter uses Session-compatible
    ModelMessage values; a new canonical schema requires a separate decision.
    """

    request_id: str
    route: str
    messages: tuple[MessageT, ...]
    instructions: str
    tools: tuple[Mapping[str, Any], ...]
    input_manifest: ModelInputManifest
    native_tools: tuple[ModelNativeTool, ...] = ()
    settings: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    #: Execution controls, not model settings or prompt contents.
    stream_idle_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("model driver request_id must not be empty")
        if self.route != self.input_manifest.route:
            raise ValueError("model driver route must match its input manifest")
        if len(self.messages) != self.input_manifest.message_count:
            raise ValueError("model driver message count must match its input manifest")
        if len(self.tools) + len(self.native_tools) != self.input_manifest.tool_count:
            raise ValueError("model driver tool count must match its input manifest")
        object.__setattr__(self, "tools", tuple(_freeze(tool) for tool in self.tools))
        object.__setattr__(self, "settings", _freeze(self.settings))


class ModelDriverStream(Protocol[StreamMessageT_co]):
    """One provider request stream and its exact terminal/partial response."""

    @property
    def events(self) -> AsyncIterator[ModelStreamEvent]: ...

    @property
    def response(self) -> StreamMessageT_co | None: ...


class ModelDriver(Protocol[MessageT]):
    """Translate model requests and own only provider transport lifecycle."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def open_stream(
        self,
        request: ModelDriverRequest[MessageT],
    ) -> AbstractAsyncContextManager[ModelDriverStream[MessageT]]: ...

    def continuation_delay(self, response: MessageT) -> float | None: ...

    async def cancel_suspended_response(self, response: MessageT) -> None: ...

    def merge_responses(self, previous: MessageT, current: MessageT) -> MessageT: ...

    def response_text(self, response: MessageT) -> str: ...

    def response_thinking(self, response: MessageT) -> str: ...


class ReplayRecording(_DriverContract):
    """A bounded deterministic stream keyed by ModelInputManifest fingerprint."""

    request_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    events: tuple[ModelStreamEvent, ...] = Field(min_length=2)
    response: Any | None = None

    @model_validator(mode="after")
    def validate_stream(self) -> ReplayRecording:
        sequences = [event.sequence for event in self.events]
        if sequences != list(range(len(self.events))):
            raise ValueError("replay event sequences must be contiguous and start at zero")
        if not isinstance(self.events[0], ModelResponseStarted):
            raise ValueError("replay stream must start with response_started")
        if not isinstance(self.events[-1], ModelResponseCompleted | ModelProviderError):
            raise ValueError("replay stream must end with response_completed or provider_error")
        return self


class ReplayMismatchError(RuntimeError):
    """Raised when a request has no exact fingerprint-matched recording."""


class ReplayModelDriver(Generic[MessageT]):
    """Offline ModelDriver used by Native Loop contract and recovery tests."""

    def __init__(self, recordings: Sequence[ReplayRecording]) -> None:
        indexed: dict[str, ReplayRecording] = {}
        for recording in recordings:
            if recording.request_fingerprint in indexed:
                raise ValueError(f"duplicate replay request fingerprint: {recording.request_fingerprint}")
            indexed[recording.request_fingerprint] = recording
        self._recordings = MappingProxyType(indexed)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def continuation_delay(self, response: MessageT) -> float | None:
        del response
        return None

    async def cancel_suspended_response(self, response: MessageT) -> None:
        del response

    def merge_responses(self, previous: MessageT, current: MessageT) -> MessageT:
        del previous
        return current

    def response_text(self, response: MessageT) -> str:
        if isinstance(response, Mapping):
            content = cast(Mapping[str, object], response).get("content")
            return content if isinstance(content, str) else ""
        return ""

    def response_thinking(self, response: MessageT) -> str:
        if isinstance(response, Mapping):
            thinking = cast(Mapping[str, object], response).get("thinking")
            return thinking if isinstance(thinking, str) else ""
        return ""

    @asynccontextmanager
    async def open_stream(
        self,
        request: ModelDriverRequest[MessageT],
    ) -> AsyncGenerator[ModelDriverStream[MessageT], None]:
        fingerprint = request.input_manifest.request_fingerprint
        recording = self._recordings.get(fingerprint)
        if recording is None:
            raise ReplayMismatchError(
                f"no replay recording for request fingerprint {fingerprint}; approximate replay is forbidden"
            )
        yield _ReplayDriverStream[MessageT](recording)


class _ReplayDriverStream(Generic[MessageT]):
    def __init__(self, recording: ReplayRecording) -> None:
        self._recording = recording

    @property
    def events(self) -> AsyncIterator[ModelStreamEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamEvent]:
        for event in self._recording.events:
            yield event

    @property
    def response(self) -> MessageT | None:
        return cast(MessageT | None, self._recording.response)


__all__ = [
    "ModelDriver",
    "ModelDriverRequest",
    "ModelDriverStream",
    "ModelNativeTool",
    "ModelProviderError",
    "ModelResponseCompleted",
    "ModelResponseStarted",
    "ModelStopReason",
    "ModelStreamEvent",
    "ModelTextDelta",
    "ModelThinkingDelta",
    "ModelToolArgumentsDelta",
    "ModelToolCallCompleted",
    "ModelToolCallStarted",
    "ModelUsage",
    "ReplayMismatchError",
    "ReplayModelDriver",
    "ReplayRecording",
]
