"""Reversible registration lifetime without owning domain state."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar, cast

Disposer = Callable[[], object]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ScopeDiagnostic:
    """Bounded cleanup evidence retained after a scope closes."""

    label: str
    error_type: str
    message: str


class RegistrationScope:
    """Own reversible registrations and background tasks for one activation.

    The scope deliberately contains no Session, Plan, Agent, or Work Product
    state. Its only authority is the lifetime of registrations added to it.
    """

    def __init__(self, name: str, *, diagnostic_limit: int = 32) -> None:
        if diagnostic_limit < 1:
            raise ValueError("diagnostic_limit must be positive")
        self.name = name
        self._diagnostic_limit = diagnostic_limit
        self._disposers: list[tuple[str, Disposer]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._diagnostics: list[ScopeDiagnostic] = []
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def active_task_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    @property
    def diagnostics(self) -> tuple[ScopeDiagnostic, ...]:
        return tuple(self._diagnostics)

    def add_disposer(self, disposer: Disposer, *, label: str | None = None) -> Disposer:
        if self._closed:
            raise RuntimeError(f"registration scope {self.name!r} is closed")
        self._disposers.append((label or getattr(disposer, "__name__", "disposer"), disposer))
        return disposer

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[T]:
        if self._closed:
            coroutine.close()
            raise RuntimeError(f"registration scope {self.name!r} is closed")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        return task

    async def close_and_wait(self) -> tuple[ScopeDiagnostic, ...]:
        """Cancel tasks, run every disposer in reverse order, and quiesce."""

        async with self._close_lock:
            if self._closed:
                return self.diagnostics
            self._closed = True
            tasks = tuple(self._tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for task, result in zip(tasks, results, strict=True):
                    if isinstance(result, BaseException) and not isinstance(
                        result, asyncio.CancelledError
                    ):
                        self._record(task.get_name(), result)
            self._tasks.clear()
            while self._disposers:
                label, disposer = self._disposers.pop()
                try:
                    result = disposer()
                    if inspect.isawaitable(result):
                        await cast(Awaitable[object], result)
                except BaseException as error:
                    self._record(label, error)
            return self.diagnostics

    def _record(self, label: str, error: BaseException) -> None:
        if len(self._diagnostics) >= self._diagnostic_limit:
            return
        self._diagnostics.append(
            ScopeDiagnostic(label, type(error).__name__, str(error)[:500])
        )


__all__ = ["Disposer", "RegistrationScope", "ScopeDiagnostic"]
