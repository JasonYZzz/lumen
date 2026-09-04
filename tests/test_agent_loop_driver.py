from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from lumen.agent_loop import (
    ModelDriverRequest,
    ModelProviderError,
    ModelResponseCompleted,
    ModelResponseStarted,
    ModelStopReason,
    ModelStreamEvent,
    ModelTextDelta,
    ReplayMismatchError,
    ReplayModelDriver,
    ReplayRecording,
)
from lumen.context import ModelInputManifest, ReplayEligibility


def _manifest(fingerprint: str) -> ModelInputManifest:
    digest = "sha256:" + "0" * 64
    return ModelInputManifest(
        session_id="session-one",
        step=1,
        route="test:model",
        provider="test",
        model="model",
        context_fingerprint="context-one",
        message_count=1,
        tool_count=0,
        instructions_digest=digest,
        message_history_digest=digest,
        tool_schema_digest=digest,
        context_sources_digest=digest,
        stable_prefix_digest=digest,
        dynamic_tail_digest=digest,
        request_fingerprint=fingerprint,
        replay_eligibility=ReplayEligibility.VERIFY_ONLY,
        non_replayable_reasons=("provider_private_framing_not_captured",),
    )


def _request(fingerprint: str) -> ModelDriverRequest[dict[str, Any]]:
    return ModelDriverRequest(
        request_id="request-one",
        route="test:model",
        messages=({"role": "user", "content": "hello"},),
        instructions="answer",
        tools=(),
        input_manifest=_manifest(fingerprint),
    )


async def test_replay_model_driver_streams_exact_recording() -> None:
    fingerprint = "sha256:" + "1" * 64
    recording = ReplayRecording(
        request_fingerprint=fingerprint,
        events=(
            ModelResponseStarted(sequence=0, provider_response_id="response-one"),
            ModelTextDelta(sequence=1, content="done"),
            ModelResponseCompleted(
                sequence=2,
                stop_reason=ModelStopReason.END_TURN,
                provider_response_id="response-one",
            ),
        ),
        response={"role": "assistant", "content": "done"},
    )
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([recording])

    async with driver.open_stream(_request(fingerprint)) as stream:
        events = [event async for event in stream.events]

    assert events == list(recording.events)
    assert stream.response == {"role": "assistant", "content": "done"}
    payload = TypeAdapter(ReplayRecording).dump_python(recording, mode="json")
    assert TypeAdapter(ReplayRecording).validate_python(payload) == recording


async def test_replay_model_driver_refuses_approximate_match() -> None:
    fingerprint = "sha256:" + "1" * 64
    recording = ReplayRecording(
        request_fingerprint=fingerprint,
        events=(
            ModelResponseStarted(sequence=0),
            ModelProviderError(sequence=1, category="provider", message="failed"),
        ),
    )
    driver: ReplayModelDriver[dict[str, Any]] = ReplayModelDriver([recording])

    with pytest.raises(ReplayMismatchError, match="approximate replay is forbidden"):
        async with driver.open_stream(_request("sha256:" + "2" * 64)) as stream:
            _ = [event async for event in stream.events]


def test_replay_recording_requires_ordered_terminal_stream() -> None:
    with pytest.raises(ValidationError, match="contiguous"):
        ReplayRecording(
            request_fingerprint="sha256:" + "1" * 64,
            events=(
                ModelResponseStarted(sequence=0),
                ModelResponseCompleted(sequence=2, stop_reason=ModelStopReason.END_TURN),
            ),
        )

    adapter: TypeAdapter[ModelStreamEvent] = TypeAdapter(ModelStreamEvent)
    parsed: ModelStreamEvent = adapter.validate_python(
        {"kind": "text_delta", "sequence": 1, "content": "hello"}
    )
    assert isinstance(parsed, ModelTextDelta)


def test_model_driver_request_is_frozen_and_matches_manifest_counts() -> None:
    fingerprint = "sha256:" + "3" * 64
    manifest = _manifest(fingerprint).model_copy(update={"tool_count": 1})
    source_tool = {
        "name": "read_file",
        "parameters": {"type": "object", "required": ["path"]},
    }
    source_settings = {"temperature": 0}
    request = ModelDriverRequest(
        request_id="request-frozen",
        route="test:model",
        messages=({"role": "user", "content": "hello"},),
        instructions="answer",
        tools=(source_tool,),
        input_manifest=manifest,
        settings=source_settings,
    )

    source_tool["name"] = "mutated"
    source_settings["temperature"] = 1
    assert request.tools[0]["name"] == "read_file"
    assert request.settings["temperature"] == 0
    with pytest.raises(TypeError):
        request.tools[0]["name"] = "forbidden"  # type: ignore[index]
    with pytest.raises(ValueError, match="route must match"):
        ModelDriverRequest(
            request_id="request-invalid",
            route="other:model",
            messages=({"role": "user", "content": "hello"},),
            instructions="answer",
            tools=(source_tool,),
            input_manifest=manifest,
        )
