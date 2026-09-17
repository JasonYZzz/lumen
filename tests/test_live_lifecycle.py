from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from lumen.completion import CompletionGate
from lumen.config import LiveConfig, PermissionsConfig
from lumen.live.bailian_realtime import BailianRealtimeAdapter
from lumen.live.events import LiveEventJournal
from lumen.live.manager import LiveSessionManager
from lumen.live.protocol import (
    LiveCompletionControl,
    LiveMediaKind,
    LiveProviderEvent,
    LiveProviderEventKind,
    LiveProviderOpenRequest,
    LiveSessionSpec,
)
from lumen.live.testing import FakeRealtimeTransport
from lumen.live.types import LiveConnectRequest, LiveEvent, LiveEventKind
from lumen.sessions import SessionRepository
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry


def _manager(tmp_path: Path) -> tuple[LiveSessionManager, FakeRealtimeTransport, str]:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    transport = FakeRealtimeTransport(
        media=LiveMediaKind.HOST_WEBSOCKET,
        completion_control=LiveCompletionControl.HOST_GATED_SYNTHESIS,
    )
    manager = LiveSessionManager(
        config=LiveConfig(enabled=True, api_key="test"),
        router=transport.router(), repository=repository,
        capability_gateway=CapabilityGateway(
            ToolRegistry(tmp_path), PermissionPolicy(PermissionsConfig()), default_timeout=1,
        ),
        completion_gate=CompletionGate(),
        plan_provider=lambda session_id: repository.load(session_id).plan,
        context_documents=lambda _session_id: (),
    )
    return manager, transport, session.id


async def test_audio_backlog_is_bounded_and_end_does_not_wait_for_a_consumer(tmp_path: Path) -> None:
    manager, transport, session_id = _manager(tmp_path)
    try:
        connected = await manager.connect(LiveConnectRequest(session_id=session_id, client_request_id="pcm"))
        connection = transport.connections[0]
        for index in range(100):
            await asyncio.wait_for(connection.emit(LiveProviderEvent(
                kind=LiveProviderEventKind.RESPONSE_AUDIO_DELTA, audio=bytes([index]),
            )), 1)
        record = manager._records[connected.live_session_id]  # pyright: ignore[reportPrivateUsage]
        assert record.audio_queue.qsize() == 64
        assert record.audio_queue.get_nowait() == bytes([36])
        # Control remains usable even without a media consumer.
        await asyncio.wait_for(manager.interrupt(connected.live_session_id), 1)
        await asyncio.wait_for(manager.end(connected.live_session_id), 1)
        assert connection.closed
        assert [item async for item in manager.subscribe_audio(connected.live_session_id)] == []
    finally:
        await manager.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_bailian_failed_session_update_closes_the_unpublished_websocket(cancel: bool) -> None:
    error = asyncio.CancelledError() if cancel else RuntimeError("session update failed")
    websocket = SimpleNamespace(send=AsyncMock(side_effect=error), close=AsyncMock())
    adapter = BailianRealtimeAdapter(
        api_key="test", workspace_id="test", model="test",
        websocket_connect=AsyncMock(return_value=websocket),
    )

    async def sink(_event: LiveProviderEvent) -> None:
        return None

    request = LiveProviderOpenRequest(
        live_session_id="live", session_id="session", spec=LiveSessionSpec(instructions="help"),
    )
    with pytest.raises(type(error)):
        await adapter.open(request, sink)
    websocket.close.assert_awaited_once()
    assert not adapter._connections  # pyright: ignore[reportPrivateUsage]
    await adapter.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_live_admission_closes_an_already_opened_provider_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    manager, transport, session_id = _manager(tmp_path)
    original = LiveEventJournal.append
    error = asyncio.CancelledError() if cancel else RuntimeError("admission failed after provider open")

    async def append(
        journal: LiveEventJournal, kind: LiveEventKind, data: dict[str, Any] | None = None,
    ) -> LiveEvent:
        if kind is LiveEventKind.SESSION_CONNECTED:
            raise error
        return await original(journal, kind, data)

    monkeypatch.setattr(LiveEventJournal, "append", append)
    request = LiveConnectRequest(session_id=session_id, client_request_id="failed")
    try:
        with pytest.raises(type(error)):
            await manager.connect(request)
        assert len(transport.connections) == 1
        assert transport.connections[0].closed
        # Idempotent callers are released and see failure, never a stuck waiter.
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(manager.connect(request), 1)
    finally:
        await manager.close()
