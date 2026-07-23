"""Frame-paced Markdown rendering for streamed model text."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from rich.markdown import Markdown as RichMarkdown
from textual.widgets import Static


class AssistantMarkdown(Static):
    """One logical Markdown document rendered without child widgets."""

    def __init__(self, source: str = "", *, classes: str = "") -> None:
        super().__init__("", classes=classes, markup=False)
        self.source = ""
        self.set_source(source)

    def set_source(self, source: str) -> None:
        self.source = source
        self.update(RichMarkdown(source))


class StreamingMarkdownController:
    """Coalesce text deltas into ordered, non-overlapping render frames.

    ``append`` is deliberately synchronous and cheap. The first pending delta
    arms one timer; later deltas never move that timer, which guarantees
    visible progress during a continuous stream instead of debounce starvation.
    """

    def __init__(
        self,
        render: Callable[[str], Awaitable[None]],
        *,
        frame_interval: float = 0.033,
        large_document_interval: float = 0.1,
        large_document_bytes: int = 32 * 1024,
    ) -> None:
        if frame_interval <= 0:
            raise ValueError("frame_interval must be positive")
        self._render = render
        self._frame_interval = frame_interval
        self._large_document_interval = large_document_interval
        self._large_document_bytes = large_document_bytes
        self._text = ""
        self._dirty = False
        self._closed = False
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task[None] | None = None
        self._render_lock = asyncio.Lock()
        self._error: BaseException | None = None

    @property
    def text(self) -> str:
        return self._text

    @property
    def pending(self) -> bool:
        return self._dirty or self._timer is not None or self._task is not None

    def append(self, delta: str) -> None:
        if self._closed:
            raise RuntimeError("streaming markdown controller is closed")
        if not delta:
            return
        self._text += delta
        self._dirty = True
        self._schedule_frame()

    def _schedule_frame(self) -> None:
        if self._timer is not None or self._task is not None or self._error is not None:
            return
        interval = (
            self._large_document_interval
            if len(self._text.encode("utf-8")) > self._large_document_bytes
            else self._frame_interval
        )
        self._timer = asyncio.get_running_loop().call_later(
            interval,
            self._start_frame,
        )

    def _start_frame(self) -> None:
        self._timer = None
        self._task = asyncio.create_task(self._render_frame())
        self._task.add_done_callback(self._frame_done)

    async def _render_frame(self) -> None:
        if not self._dirty:
            return
        async with self._render_lock:
            snapshot = self._text
            self._dirty = False
            await self._render(snapshot)

    def _frame_done(self, task: asyncio.Task[None]) -> None:
        self._task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._error = error
            return
        if self._dirty and not self._closed:
            self._schedule_frame()

    async def flush(self) -> None:
        """Render every delta appended before this method returns."""

        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        task = self._task
        if task is not None:
            await task
        if self._error is not None:
            raise self._error
        while self._dirty:
            async with self._render_lock:
                snapshot = self._text
                self._dirty = False
                await self._render(snapshot)
        if self._error is not None:
            raise self._error

    async def close(self) -> None:
        """Flush pending text and release the timer/task owned by this stream."""

        if self._closed:
            return
        await self.flush()
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


__all__ = ["AssistantMarkdown", "StreamingMarkdownController"]
