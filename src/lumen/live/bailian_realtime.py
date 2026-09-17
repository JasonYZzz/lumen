"""Alibaba Cloud Model Studio native Realtime WebSocket Adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any, cast

from lumen.live.protocol import (
    LiveBrowserHandshake,
    LiveCompletionControl,
    LiveMediaKind,
    LiveProviderCapabilities,
    LiveProviderCommand,
    LiveProviderCommandKind,
    LiveProviderEvent,
    LiveProviderEventKind,
    LiveProviderEventSink,
    LiveProviderOpenRequest,
)


class BailianRealtimeError(RuntimeError):
    pass


class _BailianConnection:
    def __init__(
        self,
        *,
        websocket: Any,
        request: LiveProviderOpenRequest,
        sink: LiveProviderEventSink,
        on_close: Callable[[_BailianConnection], None],
    ) -> None:
        self.provider_call_id = f"bailian:{request.live_session_id}"
        self.handshake = LiveBrowserHandshake(
            kind=LiveMediaKind.HOST_WEBSOCKET,
            media_path=f"/api/v1/live/{request.live_session_id}/media",
            input_sample_rate=16_000,
            output_sample_rate=24_000,
        )
        self._websocket = websocket
        self._sink = sink
        self._on_close = on_close
        self._closed = False
        self._send_lock = asyncio.Lock()
        self._task = asyncio.create_task(
            self._receive(),
            name=f"lumen-bailian-realtime-{request.live_session_id}",
        )

    async def send(self, command: LiveProviderCommand) -> None:
        if command.kind is LiveProviderCommandKind.CANCEL_RESPONSE:
            await self._send({"type": "response.cancel"})
            return
        if command.kind is LiveProviderCommandKind.SUBMIT_TOOL_RESULT:
            if not command.call_id or command.output is None:
                raise ValueError("submit_tool_result requires call_id and output")
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": command.call_id,
                        "output": json.dumps(
                            command.output,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
            )
            return
        if command.kind is LiveProviderCommandKind.CONTINUE_RESPONSE:
            event: dict[str, Any] = {"type": "response.create"}
            if command.instructions:
                event["response"] = {"instructions": command.instructions}
            await self._send(event)
            return
        if command.kind is LiveProviderCommandKind.SPEAK_APPROVED:
            raise BailianRealtimeError(
                "host-gated Bailian output is synthesized by the browser after Host approval"
            )
        raise ValueError(f"unsupported Bailian Realtime command: {command.kind}")

    async def send_audio(self, audio: bytes) -> None:
        if not audio:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(audio).decode("ascii"),
            }
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        await self._websocket.close()
        self._on_close(self)

    async def _send(self, event: Mapping[str, Any]) -> None:
        async with self._send_lock:
            await self._websocket.send(
                json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
            )

    async def _receive(self) -> None:
        try:
            async for raw in self._websocket:
                try:
                    decoded = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(decoded, Mapping):
                    continue
                event = _decode_event(cast(Mapping[str, Any], decoded))
                if event is not None:
                    await self._sink(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._sink(
                LiveProviderEvent(
                    kind=LiveProviderEventKind.TRANSPORT_ERROR,
                    message=f"{type(error).__name__}: {error}",
                )
            )


class BailianRealtimeAdapter:
    provider = "bailian"

    def __init__(
        self,
        *,
        api_key: str,
        workspace_id: str,
        model: str,
        region: str = "cn-beijing",
        base_url: str | None = None,
        completion_control: LiveCompletionControl = LiveCompletionControl.HOST_GATED_SYNTHESIS,
        timeout_seconds: float = 30.0,
        websocket_connect: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("Bailian Realtime requires a non-empty API key")
        if not workspace_id:
            raise ValueError("Bailian Realtime requires a workspace ID")
        if region not in {"cn-beijing", "ap-southeast-1"}:
            raise ValueError(f"unsupported Bailian Realtime region: {region}")
        if completion_control is LiveCompletionControl.NATIVE_REQUIRED_TOOL:
            raise ValueError("Bailian does not document native required-tool completion control")
        self._api_key = api_key
        self._workspace_id = workspace_id
        self._model = model
        self._region = region
        self._base_url = base_url
        self._completion_control = completion_control
        self._timeout_seconds = timeout_seconds
        self._websocket_connect = websocket_connect
        self._connections: set[_BailianConnection] = set()
        self.capabilities = LiveProviderCapabilities(
            media=frozenset({LiveMediaKind.HOST_WEBSOCKET}),
            function_calling=True,
            interruption=True,
            input_transcription=True,
            server_tool_authority=True,
            completion_control=completion_control,
            max_session_seconds=7_200,
        )

    async def open(
        self,
        request: LiveProviderOpenRequest,
        event_sink: LiveProviderEventSink,
    ) -> _BailianConnection:
        connect = self._websocket_connect
        if connect is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError as error:  # pragma: no cover - optional dependency
                raise BailianRealtimeError(
                    "Realtime voice dependencies are missing; install lumen-agent[live]"
                ) from error
        websocket = await connect(
            self._endpoint(),
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self._timeout_seconds,
            max_size=2**22,
        )
        try:
            await websocket.send(
                json.dumps(
                    self._session_update(request),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            connection = _BailianConnection(
                websocket=websocket,
                request=request,
                sink=event_sink,
                on_close=self._connections.discard,
            )
        except BaseException:
            with suppress(Exception):
                await websocket.close()
            raise
        self._connections.add(connection)
        return connection

    async def close(self) -> None:
        connections = tuple(self._connections)
        self._connections.clear()
        for connection in connections:
            with suppress(Exception):
                await connection.close()

    def _endpoint(self) -> str:
        if self._base_url:
            return f"{self._base_url.rstrip('/')}?model={self._model}"
        domain = f"{self._workspace_id}.{self._region}.maas.aliyuncs.com"
        return f"wss://{domain}/api-ws/v1/realtime?model={self._model}"

    def _session_update(self, request: LiveProviderOpenRequest) -> dict[str, Any]:
        spec = request.spec
        tools = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "parameters": item.get("parameters", {"type": "object"}),
                },
            }
            for item in spec.tools
            if item.get("name") != "complete_live_turn"
        ]
        turn_detection = {
            key: value
            for key, value in spec.turn_detection.items()
            if key in {"type", "threshold", "silence_duration_ms", "interrupt_response"}
        }
        session: dict[str, Any] = {
            "modalities": (
                ["text"]
                if self._completion_control is LiveCompletionControl.HOST_GATED_SYNTHESIS
                else ["text", "audio"]
            ),
            "voice": spec.voice,
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            "instructions": (
                spec.instructions
                + (
                    "\nUse the available tools whenever work or verification is required. Produce "
                    "the proposed final answer as text; Lumen will validate it and synthesize speech "
                    "only after the completion gate passes."
                    if self._completion_control is LiveCompletionControl.HOST_GATED_SYNTHESIS
                    else ""
                )
            ),
            "turn_detection": turn_detection,
            "tools": tools,
        }
        if spec.input_transcription is not None:
            session["input_audio_transcription"] = {
                "model": "qwen3-asr-flash-realtime",
                **(
                    {"language": spec.input_transcription["language"]}
                    if spec.input_transcription.get("language")
                    else {}
                ),
            }
        return {"type": "session.update", "session": session}


def _decode_event(raw: Mapping[str, Any]) -> LiveProviderEvent | None:
    event_type = str(raw.get("type", ""))
    if event_type == "error":
        error = raw.get("error")
        message = (
            str(cast(Mapping[str, Any], error).get("message", "Bailian Realtime error"))
            if isinstance(error, Mapping)
            else "Bailian Realtime error"
        )
        return LiveProviderEvent(kind=LiveProviderEventKind.TRANSPORT_ERROR, message=message)
    if event_type == "input_audio_buffer.speech_started":
        return LiveProviderEvent(kind=LiveProviderEventKind.SPEECH_STARTED)
    if event_type == "input_audio_buffer.speech_stopped":
        return LiveProviderEvent(kind=LiveProviderEventKind.SPEECH_STOPPED)
    if event_type.endswith("input_audio_transcription.delta"):
        return LiveProviderEvent(
            kind=LiveProviderEventKind.INPUT_TRANSCRIPT_DELTA,
            delta=str(raw.get("delta", "")),
        )
    if event_type.endswith("input_audio_transcription.completed"):
        return LiveProviderEvent(
            kind=LiveProviderEventKind.INPUT_TRANSCRIPT_COMPLETED,
            transcript=str(raw.get("transcript", "")),
        )
    if event_type == "response.created":
        return LiveProviderEvent(kind=LiveProviderEventKind.RESPONSE_STARTED)
    if event_type == "response.audio.delta":
        encoded = raw.get("delta")
        audio: bytes | None = None
        if isinstance(encoded, str):
            with suppress(ValueError):
                audio = base64.b64decode(encoded, validate=True)
        return LiveProviderEvent(kind=LiveProviderEventKind.RESPONSE_AUDIO_DELTA, audio=audio)
    if event_type in {"response.audio_transcript.delta", "response.text.delta"}:
        return LiveProviderEvent(
            kind=LiveProviderEventKind.RESPONSE_TRANSCRIPT_DELTA,
            delta=str(raw.get("delta", "")),
        )
    if event_type == "response.function_call_arguments.done":
        arguments_raw = raw.get("arguments", "{}")
        try:
            arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
        except json.JSONDecodeError:
            arguments = None
        return LiveProviderEvent(
            kind=LiveProviderEventKind.TOOL_CALL_READY,
            call_id=str(raw.get("call_id", "")) or None,
            name=str(raw.get("name", "")) or None,
            arguments=(
                dict(cast(Mapping[str, Any], arguments)) if isinstance(arguments, Mapping) else None
            ),
        )
    if event_type == "response.done":
        response = raw.get("response")
        data: Mapping[str, Any] = (
            cast(Mapping[str, Any], response) if isinstance(response, Mapping) else {}
        )
        usage: Any = data.get("usage")
        return LiveProviderEvent(
            kind=LiveProviderEventKind.RESPONSE_COMPLETED,
            event_id=str(raw.get("event_id", "")) or None,
            response_id=str(data.get("id", "")) or None,
            response_status=str(data.get("status", "completed")),
            usage=dict(cast(Mapping[str, Any], usage)) if isinstance(usage, Mapping) else None,
        )
    return None


__all__ = ["BailianRealtimeAdapter", "BailianRealtimeError"]
