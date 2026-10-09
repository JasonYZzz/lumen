from __future__ import annotations

import asyncio
from threading import Event

import pytest

from lumen.io import run_io


async def test_cancellation_waits_for_worker_even_after_repeated_cancellation() -> None:
    started, release, finished = asyncio.Event(), Event(), Event()
    loop = asyncio.get_running_loop()
    def write() -> str:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        finished.set()
        return "durable"
    task = asyncio.create_task(run_io(write))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
