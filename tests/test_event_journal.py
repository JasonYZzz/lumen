from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from test_workspace_host import LocalResources, _runtime  # pyright: ignore[reportPrivateUsage]

from lumen.application import (
    CancelRun,
    CreateSession,
    GetBootstrap,
    RunNotFoundError,
    StartRun,
    WorkspaceHost,
)
from lumen.application.events import EventJournal
from lumen.events import TextDelta, TextRetracted, UsageUpdated


async def test_bounded_replay_preserves_sequences_and_slow_reconnects() -> None:
    journal = EventJournal(session_id="session", run_id="run", max_bytes=512)
    for index in range(100):
        await journal.append(TextDelta(f"中文🙂 {index}"))
    await journal.append(TextRetracted(2))
    await journal.close()
    assert journal.retained_bytes <= 512
    assert len(journal.events) < 100
    replay = [event async for event in journal.subscribe(90)]
    assert [event.sequence for event in replay] == list(range(91, 102))
    assert replay[-1].data == {"characters": 2}
    assert journal.subscriber_count == 0
    journal.dispose()


async def test_subscriber_disconnect_does_not_close_publication() -> None:
    journal = EventJournal(session_id="session", run_id="run")
    await journal.append(TextDelta("first"))
    subscription = journal.subscribe()
    await anext(subscription)
    assert journal.subscriber_count == 1
    await subscription.aclose()
    assert journal.subscriber_count == 0
    await journal.append(TextDelta("next"))
    subscription = journal.subscribe(1)
    assert (await anext(subscription)).sequence == 2
    await subscription.aclose()
    await journal.close()
    journal.dispose()


async def test_live_tail_and_cold_replay_share_canonical_payload_without_subscriber_mutation() -> None:
    journal = EventJournal(session_id="session", run_id="run", max_bytes=512)
    await journal.append(UsageUpdated({"cost": Decimal("0.02")}))
    subscription = journal.subscribe()
    first = await anext(subscription)
    assert first.data["usage"]["cost"] == "0.02"
    first.data["usage"]["cost"] = "changed"
    await subscription.aclose()
    for _ in range(10):
        await journal.append(TextDelta("tail"))
    await journal.close()
    cold = [item async for item in journal.subscribe()]
    assert cold[0].data["usage"]["cost"] == "0.02"
    journal.dispose()


async def test_thousand_completed_runs_release_heavy_objects_without_replaying_expired_requests(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await host.open()
    original: StartRun | None = None
    try:
        for index in range(1000):
            created = await host.dispatch(CreateSession())
            command = StartRun(created.session_id, "fixture", f"request-{index}")
            if original is None:
                original = command
            started = await host.dispatch(command)
            events = [event async for event in host.subscribe(started.run_id)]
            assert events[-1].type == "run.completed"
        await host.dispatch(GetBootstrap())
        assert len(host._runs) <= 32  # pyright: ignore[reportPrivateUsage]
        assert len(host._sessions) <= 32  # pyright: ignore[reportPrivateUsage]
        assert len(host._requests) == 1000  # pyright: ignore[reportPrivateUsage]
        assert len(host._session_approval_keys) == 0  # pyright: ignore[reportPrivateUsage]
        assert original is not None
        with pytest.raises(RunNotFoundError, match="expired"):
            await host.dispatch(original)
        assert len(host.resources.session_repository.load(original.session_id).turns) == 1
    finally:
        await host.close()


async def test_cancellation_during_terminal_fsync_keeps_one_completed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = LocalResources(tmp_path, _runtime())
    original = resources.session_repository.append_turn
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    def append(session_id: str, **kwargs: Any) -> None:
        if kwargs.get("status") == "completed":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        original(session_id, **kwargs)
    monkeypatch.setattr(resources.session_repository, "append_turn", append)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(created.session_id, "fixture", "race"))
        async with asyncio.timeout(5):
            await entered.wait()
        cancellation = asyncio.create_task(host.dispatch(CancelRun(started.run_id)))
        await asyncio.sleep(0)
        assert not cancellation.done()
        release.set()
        await cancellation
        events = [event async for event in host.subscribe(started.run_id)]
        terminals = [event.type for event in events
                     if event.type in {"run.completed", "run.cancelled", "run.failed"}]
        assert terminals == ["run.completed"]
        assert (await host.dispatch(CancelRun(started.run_id))).status == "completed"
        assert resources.session_repository.load(created.session_id).turns[-1].status == "completed"
    finally:
        release.set()
        await host.close()
