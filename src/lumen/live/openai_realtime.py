"""OpenAI Realtime WebRTC handshake and server-side sideband Adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

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

RawEventSink = Callable[[Mapping[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _OpenAICall:
    provider_call_id: str
    answer_sdp: str


class OpenAIRealtimeError(RuntimeError):
    pass


class _OpenAIConnection:
    def __init__(
        self,
        *,
        call: _OpenAICall,
        sideband: _OpenAISideband,
        on_close: Callable[[_OpenAISideband], None],
    ) -> None:
        self.provider_call_id = call.provider_call_id
        self.handshake = LiveBrowserHandshake(
            kind=LiveMediaKind.DIRECT_WEBRTC,
            answer_sdp=call.answer_sdp,
        )
        self._sideband = sideband
        self._on_close = on_close
        self._closed = False

    async def send(self, command: LiveProviderCommand) -> None:
        if command.kind is LiveProviderCommandKind.CANCEL_RESPONSE:
            await self._sideband.send({"type": "response.cancel"})
            return
        if command.kind is LiveProviderCommandKind.SUBMIT_TOOL_RESULT:
            if not command.call_id or command.output is None:
                raise ValueError("submit_tool_result requires call_id and output")
            await self._sideband.send(
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
            response: dict[str, Any] = {}
            if command.required_tool is not None:
                response["tool_choice"] = "required" if command.required_tool else "auto"
            if command.instructions:
                response["instructions"] = command.instructions
            await self._sideband.send({"type": "response.create", "response": response})
            return
        if command.kind is LiveProviderCommandKind.SPEAK_APPROVED:
            if not command.answer:
                raise ValueError("speak_approved requires an answer")
            await self._sideband.send(
                {
                    "type": "response.create",
                    "response": {
                        "tool_choice": "none",
                        "instructions": (
                            "Speak the approved answer faithfully. Do not claim additional work, "
                            "invoke tools, or add facts. Approved answer:\n" + command.answer
                        ),
                    },
                }
            )
            return
        raise ValueError(f"unsupported OpenAI Realtime command: {command.kind}")

    async def send_audio(self, audio: bytes) -> None:
        del audio
        raise RuntimeError("audio for direct WebRTC is sent by the browser")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._sideband.close()
        self._on_close(self._sideband)


class _OpenAISideband:
    def __init__(self, websocket: Any, sink: RawEventSink) -> None:
        self._websocket = websocket
        self._sink = sink
        self._send_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._receive(), name="lumen-live-sideband")

    async def send(self, event: Mapping[str, Any]) -> None:
        async with self._send_lock:
            await self._websocket.send(json.dumps(dict(event), ensure_ascii=False, separators=(",", ":")))

    async def close(self) -> None:
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        await self._websocket.close()

    async def _receive(self) -> None:
        try:
            async for raw in self._websocket:
                try:
                    event = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(event, dict):
                    await self._sink(cast(dict[str, Any], event))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._sink(
                {
                    "type": "lumen.sideband.error",
                    "error": {"message": f"{type(error).__name__}: {error}"},
                }
            )


class OpenAIRealtimeAdapter:
    """Keep provider credentials and control traffic on the Lumen server."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = "gpt-realtime-2.1",
        timeout_seconds: float = 30.0,
        http_client: Any = None,
        websocket_connect: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI Realtime requires a non-empty API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client
        self._websocket_connect = websocket_connect
        self._connections: set[_OpenAISideband] = set()

    provider = "openai"
    capabilities = LiveProviderCapabilities(
        media=frozenset({LiveMediaKind.DIRECT_WEBRTC}),
        function_calling=True,
        interruption=True,
        input_transcription=True,
        server_tool_authority=True,
        completion_control=LiveCompletionControl.NATIVE_REQUIRED_TOOL,
        max_session_seconds=3_600,
    )

    async def open(
        self,
        request: LiveProviderOpenRequest,
        event_sink: LiveProviderEventSink,
    ) -> _OpenAIConnection:
        if not request.browser_offer_sdp:
            raise OpenAIRealtimeError("OpenAI direct WebRTC requires a browser SDP offer")
        call = await self._create_call(
            request.browser_offer_sdp,
            self._session_payload(request),
        )

        async def receive(raw: Mapping[str, Any]) -> None:
            event = self._decode_event(raw)
            if event is not None:
                await event_sink(event)

        sideband = await self._connect_sideband(call, receive)
        return _OpenAIConnection(
            call=call,
            sideband=sideband,
            on_close=self._connections.discard,
        )

    async def _create_call(self, sdp: str, session_config: Mapping[str, Any]) -> _OpenAICall:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        files = {
            "sdp": (None, sdp, "application/sdp"),
            "session": (
                None,
                json.dumps(session_config, ensure_ascii=False, separators=(",", ":")),
                "application/json",
            ),
        }
        if self._http_client is not None:
            response = await self._http_client.post(
                f"{self._base_url}/v1/realtime/calls",
                headers=headers,
                files=files,
            )
        else:
            try:
                import httpx
            except ImportError as error:  # pragma: no cover - depends on optional install
                raise OpenAIRealtimeError(
                    "Realtime voice dependencies are missing; install lumen-agent[live]"
                ) from error
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/v1/realtime/calls",
                    headers=headers,
                    files=files,
                )
        if response.status_code >= 400:
            raise OpenAIRealtimeError(
                f"OpenAI Realtime call creation failed ({response.status_code}): {response.text[:500]}"
            )
        location = response.headers.get("location")
        if not location:
            raise OpenAIRealtimeError("OpenAI Realtime response did not include a call location")
        call_id = location.rstrip("/").rsplit("/", 1)[-1]
        if not call_id.startswith("rtc_"):
            raise OpenAIRealtimeError("OpenAI Realtime returned an invalid call identifier")
        return _OpenAICall(provider_call_id=call_id, answer_sdp=response.text)

    async def _connect_sideband(
        self,
        call: _OpenAICall,
        event_sink: RawEventSink,
    ) -> _OpenAISideband:
        connect = self._websocket_connect
        if connect is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError as error:  # pragma: no cover - depends on optional install
                raise OpenAIRealtimeError(
                    "Realtime voice dependencies are missing; install lumen-agent[live]"
                ) from error

        parsed = urlsplit(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        sideband_url = urlunsplit(
            (scheme, parsed.netloc, "/v1/realtime", f"call_id={call.provider_call_id}", "")
        )
        websocket = await connect(
            sideband_url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            open_timeout=self._timeout_seconds,
            max_size=2**20,
        )
        connection = _OpenAISideband(websocket, event_sink)
        self._connections.add(connection)
        return connection

    async def close(self) -> None:
        connections = tuple(self._connections)
        self._connections.clear()
        for connection in connections:
            with suppress(Exception):
                await connection.close()

    def _session_payload(self, request: LiveProviderOpenRequest) -> dict[str, Any]:
        spec = request.spec
        audio_input: dict[str, Any] = {"turn_detection": spec.turn_detection}
        if spec.input_transcription is not None:
            audio_input["transcription"] = spec.input_transcription
        payload: dict[str, Any] = {
            "type": "realtime",
            "model": self._model,
            "instructions": (
                spec.instructions
                + (
                    "\nDuring the work phase use tools. When ready to answer, call "
                    "complete_live_turn with the full final answer. Never speak the final answer "
                    "before that tool succeeds."
                    if spec.strict_completion
                    else ""
                )
            ),
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": {"voice": spec.voice},
            },
            "tools": list(spec.tools),
            "tool_choice": "required" if spec.strict_completion else "auto",
        }
        if spec.reasoning_effort:
            payload["reasoning"] = {"effort": spec.reasoning_effort}
        return payload

    @staticmethod
    def _decode_event(raw: Mapping[str, Any]) -> LiveProviderEvent | None:
        event_type = str(raw.get("type", ""))
        if event_type in {"lumen.sideband.error", "error"}:
            error = raw.get("error")
            message = (
                str(cast(Mapping[str, Any], error).get("message", "Realtime provider error"))
                if isinstance(error, Mapping)
                else "Realtime provider error"
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
        if event_type in {"response.output_audio.delta", "response.audio.delta"}:
            encoded = raw.get("delta")
            audio: bytes | None = None
            if isinstance(encoded, str):
                with suppress(ValueError):
                    audio = base64.b64decode(encoded, validate=True)
            return LiveProviderEvent(kind=LiveProviderEventKind.RESPONSE_AUDIO_DELTA, audio=audio)
        if event_type in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
            "response.text.delta",
        }:
            return LiveProviderEvent(
                kind=LiveProviderEventKind.RESPONSE_TRANSCRIPT_DELTA,
                delta=str(raw.get("delta", "")),
            )
        if event_type == "response.function_call_arguments.done":
            arguments_raw = raw.get("arguments", "{}")
            try:
                arguments = (
                    json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                )
            except json.JSONDecodeError:
                arguments = None
            return LiveProviderEvent(
                kind=LiveProviderEventKind.TOOL_CALL_READY,
                call_id=str(raw.get("call_id", "")) or None,
                name=str(raw.get("name", "")) or None,
                arguments=(
                    dict(cast(Mapping[str, Any], arguments))
                    if isinstance(arguments, Mapping)
                    else None
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


__all__ = ["OpenAIRealtimeAdapter", "OpenAIRealtimeError"]
