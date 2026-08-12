"""Deterministic Provider Adapter used by Live contract and Host tests."""

from __future__ import annotations

from lumen.live.protocol import (
    LiveBrowserHandshake,
    LiveCompletionControl,
    LiveMediaKind,
    LiveProviderCapabilities,
    LiveProviderCommand,
    LiveProviderEvent,
    LiveProviderEventSink,
    LiveProviderOpenRequest,
)
from lumen.live.router import LiveProviderRouter, LiveRouteProfile


class FakeProviderConnection:
    def __init__(
        self,
        sink: LiveProviderEventSink,
        *,
        provider_call_id: str,
        handshake: LiveBrowserHandshake,
    ) -> None:
        self.provider_call_id = provider_call_id
        self.handshake = handshake
        self.sink = sink
        self.sent: list[LiveProviderCommand] = []
        self.audio: list[bytes] = []
        self.closed = False

    async def send(self, command: LiveProviderCommand) -> None:
        self.sent.append(command)

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def emit(self, event: LiveProviderEvent) -> None:
        await self.sink(event)

    async def close(self) -> None:
        self.closed = True


class FakeRealtimeTransport:
    """Compatibility name for the canonical fake Provider Adapter."""

    provider = "fake"
    def __init__(
        self,
        *,
        answer_sdp: str = "v=0\r\nfake-answer",
        media: LiveMediaKind = LiveMediaKind.DIRECT_WEBRTC,
        completion_control: LiveCompletionControl = LiveCompletionControl.NATIVE_REQUIRED_TOOL,
    ) -> None:
        self.answer_sdp = answer_sdp
        self.media = media
        self.capabilities = LiveProviderCapabilities(
            media=frozenset({media}),
            function_calling=True,
            interruption=True,
            input_transcription=True,
            server_tool_authority=True,
            completion_control=completion_control,
            max_session_seconds=3_600,
        )
        self.requests: list[LiveProviderOpenRequest] = []
        self.connections: list[FakeProviderConnection] = []
        self.closed = False

    async def open(
        self,
        request: LiveProviderOpenRequest,
        event_sink: LiveProviderEventSink,
    ) -> FakeProviderConnection:
        self.requests.append(request)
        connection = FakeProviderConnection(
            event_sink,
            provider_call_id=f"rtc_fake_{len(self.requests)}",
            handshake=(
                LiveBrowserHandshake(
                    kind=LiveMediaKind.DIRECT_WEBRTC,
                    answer_sdp=self.answer_sdp,
                )
                if self.media is LiveMediaKind.DIRECT_WEBRTC
                else LiveBrowserHandshake(
                    kind=LiveMediaKind.HOST_WEBSOCKET,
                    media_path=f"/api/v1/live/{request.live_session_id}/media",
                    input_sample_rate=16_000,
                    output_sample_rate=24_000,
                )
            ),
        )
        self.connections.append(connection)
        return connection

    async def close(self) -> None:
        self.closed = True
        for connection in self.connections:
            await connection.close()

    def router(self) -> LiveProviderRouter:
        return LiveProviderRouter(
            routes={
                "fake": (
                    LiveRouteProfile(name="fake", provider=self.provider, model="fake-realtime"),
                    self,
                )
            },
            default_route="fake",
        )


FakeSidebandConnection = FakeProviderConnection

__all__ = ["FakeProviderConnection", "FakeRealtimeTransport", "FakeSidebandConnection"]
