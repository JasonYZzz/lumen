"""Frame-paced Markdown rendering for streamed model text."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

from rich.markdown import Markdown as RichMarkdown
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

# A fenced code block opens on a line whose first non-space characters are a
# run of at least three backticks or tildes (up to three leading spaces).
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# A fence only closes on a bare run of the same marker, at least as long as
# the opening run; an info string means it opens a nested fence instead.
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")
# Blocks whose standalone rich rendering already starts with a blank line:
# lists, block quotes and tables pad themselves, so they need no extra
# separator before them.
_SELF_LEAD_RE = re.compile(r"^ {0,3}(>|[-+*] |\d{1,9}[.)] |\|)")
# A block that is a single horizontal rule renders its own trailing blank
# line and suppresses the separator after it (``new_line = False`` in rich).
_HRULE_RE = re.compile(r"^ {0,3}(-\s*){3,}$|^ {0,3}(\*\s*){3,}$|^ {0,3}(_\s*){3,}$")
# Link reference definitions (``[label]: destination``). They are
# document-global in Markdown and render nothing themselves.
_LINK_DEF_RE = re.compile(r"^ {0,3}\[[^\]]+\]:[^\n]*", re.MULTILINE)


def _link_definition_suffix(source: str) -> str:
    """Return all link definition lines in *source* as an appendable suffix.

    Reference-style links (``[text][label]``) may be resolved by a definition
    anywhere in the document, including text that streams in after the block
    using them was frozen. Appending the definitions to each block's source
    keeps the split rendering consistent with the single document; the
    definitions themselves produce no visible output.
    """

    definitions = _LINK_DEF_RE.findall(source)
    return "\n\n" + "\n".join(definitions) if definitions else ""


def _has_open_fence(text: str) -> bool:
    """Return True if *text* ends inside an unclosed fenced code block."""

    fence_marker = ""
    fence_length = 0
    for line in text.split("\n"):
        if fence_marker:
            closing = _FENCE_CLOSE_RE.match(line)
            if (
                closing is not None
                and closing.group(1)[0] == fence_marker
                and len(closing.group(1)) >= fence_length
            ):
                fence_marker = ""
        else:
            opening = _FENCE_OPEN_RE.match(line)
            if opening is not None:
                fence_marker = opening.group(1)[0]
                fence_length = len(opening.group(1))
    return bool(fence_marker)


def _split_blocks(source: str) -> list[str]:
    """Split *source* into top-level blocks at blank lines outside fences.

    Blank lines inside an unclosed fenced code block are literal content and
    never close a block. Blank separator lines themselves are dropped; each
    returned block is the exact source slice that produced it.
    """

    blocks: list[str] = []
    current: list[str] = []
    fence_marker = ""
    fence_length = 0
    for line in source.split("\n"):
        if not line.strip() and not fence_marker:
            # A blank line outside a fence closes the current block.
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
        if fence_marker:
            closing = _FENCE_CLOSE_RE.match(line)
            if (
                closing is not None
                and closing.group(1)[0] == fence_marker
                and len(closing.group(1)) >= fence_length
            ):
                fence_marker = ""
        else:
            opening = _FENCE_OPEN_RE.match(line)
            if opening is not None:
                fence_marker = opening.group(1)[0]
                fence_length = len(opening.group(1))
    if current:
        blocks.append("\n".join(current))
    return blocks


def _partition_blocks(blocks: list[str]) -> tuple[list[str], str]:
    """Split blocks into a freezable prefix and the still-open tail text.

    The last block is always open while the stream runs. An earlier block may
    only freeze once its successor's first line is known and is not indented:
    an indented successor can still merge into it (a loose list continuation
    or an indented code block), which would change how the frozen block
    renders. The tail is re-joined with blank lines so it renders exactly as
    the corresponding slice of the single document.
    """

    if not blocks:
        return [], ""
    cut = len(blocks) - 1
    for index in range(len(blocks) - 1):
        if blocks[index + 1].startswith((" ", "\t")):
            cut = index
            break
    return blocks[:cut], "\n\n".join(blocks[cut:])


def _needs_top_margin(previous: str | None, block: str) -> bool:
    """Return True if *block* needs a one-line top margin after *previous*.

    A single rich Markdown document yields exactly one blank line between
    consecutive top-level elements, unless the previous element suppresses it
    (a horizontal rule). Lists, quotes and tables render their own leading
    blank line, which already satisfies that separator; every other block
    type needs an explicit one-line margin so the split rendering stays
    visually identical to the single-document rendering.
    """

    if previous is None or _HRULE_RE.match(previous):
        return False
    return _SELF_LEAD_RE.match(block) is None


class AssistantMarkdown(Vertical):
    """One logical Markdown document rendered block-by-block.

    The source is split into top-level blocks (see ``_split_blocks``). Blocks
    that can no longer change are frozen into static children that are parsed
    once and never touched again; only the trailing open block is re-rendered
    when new text arrives. Streaming parse cost therefore stays proportional
    to the open tail instead of the whole document.
    """

    DEFAULT_CSS = """
    AssistantMarkdown {
        width: 1fr;
        height: auto;
        layout: vertical;
    }
    """

    def __init__(self, source: str = "", *, classes: str = "") -> None:
        super().__init__(classes=classes)
        self.source = ""
        self._frozen: list[str] = []
        self._tail: Static | None = None
        self._link_suffix = ""
        self.set_source(source)

    def compose(self) -> ComposeResult:
        # Children are materialised at mount time because Textual forbids
        # mounting onto a widget that is not mounted itself.
        self._link_suffix = _link_definition_suffix(self.source)
        frozen, tail_text = _partition_blocks(_split_blocks(self.source))
        previous: str | None = None
        for block in frozen:
            yield self._block_widget(block, previous)
            previous = block
        self._frozen = frozen
        self._tail = self._block_widget(tail_text, previous)
        yield self._tail

    def set_source(self, source: str) -> None:
        """Replace the full document source (append-only while streaming)."""

        self.source = source
        if self.is_mounted:
            self._sync_blocks()

    def _sync_blocks(self) -> None:
        """Freeze newly closed blocks and re-render the open tail."""

        frozen, tail_text = _partition_blocks(_split_blocks(self.source))
        if frozen[: len(self._frozen)] != self._frozen:
            # The source changed other than by appending (e.g. a rebuilt
            # timeline); drop all children and rebuild from scratch.
            for child in list(self.children):
                child.remove()
            self._frozen = []
            self._tail = None
        link_suffix = _link_definition_suffix(self.source)
        if link_suffix != self._link_suffix:
            self._link_suffix = link_suffix
            # A new link definition can resolve reference-style links inside
            # already-frozen blocks; re-render just those.
            for child, block in zip(list(self.children), self._frozen, strict=False):
                if "[" in block:
                    child.update(self._render_block(block))
        if self._tail is None:
            self._tail = self._block_widget(tail_text, None)
            self.mount(self._tail)
        previous: str | None = self._frozen[-1] if self._frozen else None
        for block in frozen[len(self._frozen) :]:
            self.mount(self._block_widget(block, previous), before=self._tail)
            previous = block
        self._frozen = frozen
        self._tail.set_class(_needs_top_margin(previous, tail_text), "assistant-block--spaced")
        # Inside an unclosed fence the appended definitions would show up as
        # literal code, so the tail only gets them once its fences balance.
        if _has_open_fence(tail_text):
            self._tail.update(RichMarkdown(tail_text))
        else:
            self._tail.update(self._render_block(tail_text))

    def _block_widget(self, text: str, previous: str | None) -> Static:
        """Create one block child, with a spacer class when rich would not
        supply the inter-block blank line itself."""

        classes = "assistant-block"
        if _needs_top_margin(previous, text):
            classes += " assistant-block--spaced"
        return Static(self._render_block(text), classes=classes, markup=False)

    def _render_block(self, text: str) -> RichMarkdown:
        """Parse one block with the document's link definitions appended."""

        return RichMarkdown(text + self._link_suffix)


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
