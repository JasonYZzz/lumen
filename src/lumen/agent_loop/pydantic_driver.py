"""PydanticAI low-level Model Adapter for :class:`LumenAgentLoop`.

This Implementation deliberately uses the public ``Model.request_stream`` seam rather than
the high-level Agent graph. PydanticAI owns provider wire translation; Lumen
owns request evidence, tool execution, retry, cancellation and completion.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self, cast

import httpx
from anthropic import APIConnectionError as AnthropicConnectionError
from openai import APIConnectionError as OpenAIConnectionError
from pydantic_ai.exceptions import (
    ContentFilterError,
    IncompleteToolCall,
    ModelAPIError,
    ModelHTTPError,
)
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelResponse,
    NativeToolCallPart,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_ai.models import Model, ModelRequestParameters, infer_model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.profiles.openai import OPENAI_REASONING_EFFORT_MAP
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from lumen.agent_loop.driver import (
    ModelDriverRequest,
    ModelDriverStream,
    ModelProviderError,
    ModelResponseCompleted,
    ModelResponseStarted,
    ModelStopReason,
    ModelStreamEvent,
    ModelTextDelta,
    ModelThinkingDelta,
    ModelToolArgumentsDelta,
    ModelToolCallCompleted,
    ModelToolCallStarted,
    ModelUsage,
)


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, Any], value)
        return {str(key): _plain_json(item) for key, item in mapping.items()}
    if isinstance(value, tuple | list):
        sequence = cast(tuple[Any, ...] | list[Any], value)
        return [_plain_json(item) for item in sequence]
    return value


def _tool_definition(document: Mapping[str, Any]) -> ToolDefinition:
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("model-visible tool schema requires a non-empty name")
    description = document.get("description")
    parameters = _plain_json(document.get("parameters") or {})
    returns = _plain_json(document.get("returns"))
    tool_kind = document.get("tool_kind")
    if not isinstance(parameters, dict):
        raise TypeError(f"tool {name!r} parameters must be a JSON object schema")
    if returns is not None and not isinstance(returns, dict):
        raise TypeError(f"tool {name!r} returns must be a JSON object schema")
    return ToolDefinition(
        name=name,
        description=description if isinstance(description, str) else None,
        parameters_json_schema=cast(dict[str, Any], parameters),
        return_schema=cast(dict[str, Any] | None, returns),
        sequential=False,
        tool_kind=cast(Any, tool_kind) if isinstance(tool_kind, str) else None,
    )


def _stop_reason(
    finish_reason: str | None,
    *,
    response_state: str,
    has_tool_calls: bool,
) -> ModelStopReason:
    if response_state == "suspended":
        return ModelStopReason.SUSPENDED
    if response_state == "incomplete":
        return ModelStopReason.LENGTH
    if response_state == "interrupted":
        return ModelStopReason.ERROR
    if finish_reason == "length":
        return ModelStopReason.LENGTH
    if finish_reason == "content_filter":
        return ModelStopReason.CONTENT_FILTER
    if finish_reason == "refusal":
        return ModelStopReason.REFUSAL
    if finish_reason == "tool_call" or has_tool_calls:
        return ModelStopReason.TOOL_CALL
    if finish_reason == "error":
        return ModelStopReason.ERROR
    if finish_reason == "stop" or (
        finish_reason is None and response_state == "complete" and not has_tool_calls
    ):
        return ModelStopReason.END_TURN
    return ModelStopReason.UNKNOWN


def _retryable(error: BaseException) -> bool:
    if isinstance(
        error, ConnectionError | TimeoutError | httpx.TransportError
        | OpenAIConnectionError | AnthropicConnectionError,
    ):
        return True
    if isinstance(error, ModelHTTPError):
        body = error.body
        if isinstance(body, dict):
            detail = cast(dict[str, Any], body).get("error", body)
            if isinstance(detail, dict) and cast(dict[str, Any], detail).get("code") in {
                "insufficient_quota", "billing_hard_limit_reached", "usage_limit_reached",
            }:
                return False
        return error.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    return False


def _error_category(error: BaseException) -> str:
    if isinstance(error, ModelHTTPError):
        body = json.dumps(error.body, default=str).casefold()
        if error.status_code in {400, 413} and any(
            marker in body for marker in (
                "context_length_exceeded", "prompt is too long", "maximum context length",
                "context window", "input token count exceeds",
            )
        ):
            return "context_overflow"
        return f"http_{error.status_code}"
    if isinstance(error, TimeoutError | httpx.TimeoutException):
        return "timeout"
    if isinstance(
        error, ConnectionError | httpx.TransportError | OpenAIConnectionError | AnthropicConnectionError,
    ):
        return "connection"
    if isinstance(error, ModelAPIError):
        return "provider_api"
    return type(error).__name__


def _retry_after(error: BaseException) -> float | None:
    """Read only retry timing, never retain response/auth headers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if isinstance(response, httpx.Response):
            value = response.headers.get("retry-after")
            if value is not None:
                try:
                    delay = float(value)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        return None
                return max(0.0, delay) if math.isfinite(delay) else None
        current = current.__cause__
    return None


class PydanticAIModelDriver:
    """Translate one frozen Lumen request through PydanticAI's low-level Model."""

    def __init__(self, model: Model | str) -> None:
        self._model = infer_model(model)
        if isinstance(self._model, OpenAIChatModel | OpenAIResponsesModel | AnthropicModel):
            # The native Loop is the only retry authority. SDK retries would
            # hide attempts, backoff and quota failures from it.
            self._model.client.max_retries = 0
        self._lifecycle_lock = asyncio.Lock()
        self._lifecycle_leases = 0

    @property
    def manages_stream_idle_timeout(self) -> bool:
        return isinstance(self._model, OpenAIChatModel | OpenAIResponsesModel | AnthropicModel)

    async def __aenter__(self) -> Self:
        async with self._lifecycle_lock:
            if self._lifecycle_leases == 0:
                await self._model.__aenter__()
            self._lifecycle_leases += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        async with self._lifecycle_lock:
            if self._lifecycle_leases <= 0:
                raise RuntimeError("PydanticAIModelDriver lifecycle is not open")
            self._lifecycle_leases -= 1
            if self._lifecycle_leases == 0:
                return await self._model.__aexit__(exc_type, exc_value, traceback)
        return None

    def continuation_delay(self, response: ModelMessage) -> float | None:
        if not isinstance(response, ModelResponse):
            raise TypeError("suspended continuation requires ModelResponse")
        return self._model.continuation_delay(response)

    async def cancel_suspended_response(self, response: ModelMessage) -> None:
        if not isinstance(response, ModelResponse):
            raise TypeError("suspended cancellation requires ModelResponse")
        await self._model.cancel_suspended_response(response)

    def merge_responses(self, previous: ModelMessage, current: ModelMessage) -> ModelMessage:
        if not isinstance(previous, ModelResponse) or not isinstance(current, ModelResponse):
            raise TypeError("provider response merge requires ModelResponse values")
        same_response = bool(
            previous.provider_response_id
            and previous.provider_response_id == current.provider_response_id
        )
        different_model = bool(
            previous.model_name
            and current.model_name
            and previous.model_name != current.model_name
        )
        if same_response or different_model:
            merged = current
        else:
            merged = dataclasses.replace(
                current,
                parts=[*previous.parts, *current.parts],
                usage=previous.usage + current.usage,
                provider_response_id=current.provider_response_id or previous.provider_response_id,
            )
        if previous.provider_details:
            merged = dataclasses.replace(
                merged,
                provider_details={
                    **previous.provider_details,
                    **(merged.provider_details or {}),
                },
            )
        if previous.metadata:
            merged = dataclasses.replace(
                merged,
                metadata={**previous.metadata, **(merged.metadata or {})},
            )
        return merged

    def response_text(self, response: ModelMessage) -> str:
        if not isinstance(response, ModelResponse):
            return ""
        return "".join(part.content for part in response.parts if isinstance(part, TextPart))

    def response_thinking(self, response: ModelMessage) -> str:
        if not isinstance(response, ModelResponse):
            return ""
        return "".join(
            _raw_thinking_content(part) + part.content
            for part in response.parts if isinstance(part, ThinkingPart)
        )

    @asynccontextmanager
    async def open_stream(
        self,
        request: ModelDriverRequest[ModelMessage],
    ) -> AsyncGenerator[ModelDriverStream[ModelMessage], None]:
        parameters = ModelRequestParameters(
            function_tools=[_tool_definition(document) for document in request.tools],
            native_tools=[
                WebSearchTool(search_context_size=tool.search_context_size)
                for tool in request.native_tools
                if tool.kind == "web_search"
            ],
            allow_text_output=True,
            instruction_parts=([] if not request.instructions else [InstructionPart(request.instructions)]),
        )
        settings = cast(ModelSettings, _plain_json(request.settings))
        # The SDK omits Anthropic thinking when the unified setting is False.
        # An omitted field enables thinking on some compatible providers. Keep
        # the caller's explicit off choice distinct from upstream defaults.
        if isinstance(self._model, AnthropicModel) and settings.get("thinking") is False:
            if "anthropic_thinking" not in settings:
                cast(dict[str, Any], settings)["anthropic_thinking"] = {"type": "disabled"}
        # OpenAI-compatible Responses models outside the SDK's known profiles
        # otherwise silently lose an explicit unified thinking choice. Preserve
        # omission as upstream default and keep native settings authoritative.
        if (
            isinstance(self._model, OpenAIResponsesModel)
            and not self._model.profile.get("supports_thinking", False)
            and not self._model.profile.get("thinking_always_enabled", False)
            and (thinking := settings.get("thinking")) is not None
            and "openai_reasoning_effort" not in settings
        ):
            cast(dict[str, Any], settings)["openai_reasoning_effort"] = OPENAI_REASONING_EFFORT_MAP[thinking]
        if self.manages_stream_idle_timeout:
            settings["timeout"] = httpx.Timeout(
                request.stream_idle_timeout_seconds,
                connect=min(10.0, request.stream_idle_timeout_seconds),
            )
        prepared_messages = self._model.prepare_messages(list(request.messages))
        opened = False
        try:
            async with self._model.request_stream(
                prepared_messages,
                settings or None,
                parameters,
            ) as response:
                opened = True
                yield _PydanticDriverStream(response)
        except asyncio.CancelledError:
            raise
        except ContentFilterError:
            yield _StaticDriverStream(
                (
                    ModelResponseStarted(sequence=0),
                    ModelResponseCompleted(
                        sequence=1,
                        stop_reason=ModelStopReason.CONTENT_FILTER,
                    ),
                )
            )
        except IncompleteToolCall:
            yield _StaticDriverStream(
                (
                    ModelResponseStarted(sequence=0),
                    ModelResponseCompleted(sequence=1, stop_reason=ModelStopReason.LENGTH),
                )
            )
        except Exception as error:
            if opened:
                raise
            yield _StaticDriverStream(
                (
                    ModelResponseStarted(sequence=0),
                    ModelProviderError(
                        sequence=1,
                        category=_error_category(error),
                        message=(str(error).strip()
                                 or f"Provider failed ({type(error).__name__}) without details."),
                        retryable=_retryable(error),
                        retry_after_seconds=_retry_after(error),
                    ),
                )
            )


class _StaticDriverStream:
    def __init__(self, events: tuple[ModelStreamEvent, ...]) -> None:
        self._events_value = events

    @property
    def response(self) -> ModelMessage | None:
        return None

    @property
    def events(self) -> AsyncIterator[ModelStreamEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamEvent]:
        for event in self._events_value:
            yield event


class _PydanticDriverStream:
    def __init__(self, streamed: Any) -> None:
        self._streamed = streamed
        self._response: ModelResponse | None = None

    @property
    def response(self) -> ModelMessage | None:
        if self._response is not None:
            return self._response
        try:
            return cast(ModelResponse, self._streamed.get())
        except (AssertionError, RuntimeError):
            return None

    @property
    def events(self) -> AsyncIterator[ModelStreamEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ModelStreamEvent]:
        sequence = 0
        started = False
        has_tool_calls = False
        replay_safe = True
        last_usage: tuple[int, int, int | None, int | None] | None = None
        try:
            started = True
            yield ModelResponseStarted(
                    sequence=sequence,
                    provider_response_id=self._streamed.provider_response_id,
                )
            sequence += 1
            completed_calls: set[str] = set()
            started_tool_calls: set[str] = set()
            thinking_parts: dict[int, ThinkingPart] = {}
            async for event in self._streamed:
                    usage = self._streamed.usage
                    counters = (
                        usage.input_tokens, usage.output_tokens,
                        usage.cache_read_tokens, usage.cache_write_tokens,
                    )
                    if counters != last_usage:
                        yield ModelUsage(
                            sequence=sequence, input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens, cache_read_tokens=usage.cache_read_tokens,
                            cache_write_tokens=usage.cache_write_tokens,
                        )
                        sequence += 1
                        last_usage = counters
                    if isinstance(event, PartStartEvent):
                        part = event.part
                        if isinstance(part, NativeToolCallPart):
                            replay_safe = False
                        if isinstance(part, TextPart) and part.content:
                            yield ModelTextDelta(sequence=sequence, content=part.content)
                            sequence += 1
                        elif isinstance(part, ThinkingPart):
                            thinking_parts[event.index] = part
                            thinking_text = _raw_thinking_content(part) + part.content
                            if thinking_text:
                                yield ModelThinkingDelta(sequence=sequence, content=thinking_text)
                                sequence += 1
                        elif isinstance(part, ToolCallPart):
                            has_tool_calls = True
                            if part.tool_call_id not in started_tool_calls:
                                yield ModelToolCallStarted(
                                    sequence=sequence,
                                    call_id=part.tool_call_id,
                                    name=part.tool_name,
                                )
                                sequence += 1
                                started_tool_calls.add(part.tool_call_id)
                    elif isinstance(event, PartDeltaEvent):
                        delta = event.delta
                        if isinstance(delta, TextPartDelta) and delta.content_delta:
                            yield ModelTextDelta(sequence=sequence, content=delta.content_delta)
                            sequence += 1
                        elif isinstance(delta, ThinkingPartDelta):
                            previous = thinking_parts[event.index]
                            updated = delta.apply(previous)
                            thinking_parts[event.index] = updated
                            previous_raw = _raw_thinking_content(previous)
                            updated_raw = _raw_thinking_content(updated)
                            # Responses providers can stream reasoning_text into SDK
                            # raw_content instead of content_delta. Project only its
                            # new text; leave canonical provider details untouched for
                            # subsequent tool-result requests and Session replay.
                            raw_delta = (
                                updated_raw[len(previous_raw):]
                                if updated_raw.startswith(previous_raw) else ""
                            )
                            thinking_text = raw_delta + (delta.content_delta or "")
                            if thinking_text:
                                yield ModelThinkingDelta(sequence=sequence, content=thinking_text)
                                sequence += 1
                        elif isinstance(delta, ToolCallPartDelta) and delta.args_delta:
                            arguments_delta = (
                                delta.args_delta
                                if isinstance(delta.args_delta, str)
                                else json.dumps(delta.args_delta, ensure_ascii=False, sort_keys=True)
                            )
                            call_id = delta.tool_call_id
                            # Native server tools can project a ToolCallPartDelta even
                            # though their start is a NativeToolCallPart. Do not leak
                            # that delta into Lumen's client-executed function stream.
                            # If a compatible SDK omits a local PartStartEvent, the
                            # PartEndEvent below synthesizes the complete ordered pair.
                            if call_id and call_id in started_tool_calls:
                                yield ModelToolArgumentsDelta(
                                    sequence=sequence,
                                    call_id=call_id,
                                    arguments_delta=arguments_delta,
                                )
                                sequence += 1
                    elif isinstance(event, PartEndEvent) and isinstance(
                        event.part,
                        ToolCallPart,
                    ):
                        part = event.part
                        if part.tool_call_id not in completed_calls:
                            if part.tool_call_id not in started_tool_calls:
                                has_tool_calls = True
                                yield ModelToolCallStarted(
                                    sequence=sequence,
                                    call_id=part.tool_call_id,
                                    name=part.tool_name,
                                )
                                sequence += 1
                                started_tool_calls.add(part.tool_call_id)
                            yield ModelToolCallCompleted(
                                sequence=sequence,
                                call_id=part.tool_call_id,
                                name=part.tool_name,
                                arguments=part.args_as_dict(),
                            )
                            sequence += 1
                            completed_calls.add(part.tool_call_id)

            final = self._streamed.get()
            self._response = final
            usage = final.usage
            yield ModelUsage(
                    sequence=sequence,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    provider_details=dict(usage.details),
                )
            sequence += 1
            if final.state == "interrupted" or final.finish_reason == "error":
                yield ModelProviderError(
                    sequence=sequence, category="stream_disconnected",
                    message="Provider response was interrupted before completion.",
                    retryable=True, replay_safe=replay_safe,
                )
                return
            yield ModelResponseCompleted(
                    sequence=sequence,
                    stop_reason=_stop_reason(
                        final.finish_reason,
                        response_state=final.state,
                        has_tool_calls=has_tool_calls,
                    ),
                    provider_response_id=final.provider_response_id,
                )
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._streamed.cancel())
            except (NotImplementedError, RuntimeError):
                pass
            raise
        except ContentFilterError:
            if not started:
                yield ModelResponseStarted(sequence=sequence)
                sequence += 1
            yield ModelResponseCompleted(
                sequence=sequence,
                stop_reason=ModelStopReason.CONTENT_FILTER,
            )
        except IncompleteToolCall:
            if not started:
                yield ModelResponseStarted(sequence=sequence)
                sequence += 1
            yield ModelResponseCompleted(sequence=sequence, stop_reason=ModelStopReason.LENGTH)
        except Exception as error:
            if not started:
                yield ModelResponseStarted(sequence=sequence)
                sequence += 1
            yield ModelProviderError(
                sequence=sequence,
                category=_error_category(error),
                message=str(error).strip() or f"Provider failed ({type(error).__name__}) without details.",
                retryable=_retryable(error),
                retry_after_seconds=_retry_after(error),
                replay_safe=replay_safe,
            )


def _raw_thinking_content(part: ThinkingPart) -> str:
    """Read the SDK's Responses reasoning text, never signatures or opaque metadata."""
    raw: object = (part.provider_details or {}).get("raw_content")
    if not isinstance(raw, list):
        return ""
    return "".join(item for item in cast(list[object], raw) if isinstance(item, str))


__all__ = ["PydanticAIModelDriver"]
