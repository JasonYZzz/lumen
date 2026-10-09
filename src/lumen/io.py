"""Blocking storage boundary whose worker finishes before cancellation escapes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_io(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError as error:
            if worker.cancelled():
                raise
            cancelled = error
    if cancelled is not None:
        raise cancelled
    return result
