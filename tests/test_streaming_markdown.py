from __future__ import annotations

import asyncio
from typing import Any

import pytest
from rich.markdown import Markdown as RichMarkdown
from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Static

import lumen.ui.streaming_markdown as streaming_markdown
from lumen.ui.streaming_markdown import (
    AssistantMarkdown,
    StreamingMarkdownController,
    _partition_blocks,
    _split_blocks,
)

# These tests intentionally exercise the private block partitioning contract.
# pyright: reportPrivateUsage=false


class _DocApp(App[None]):
    """Minimal host mounting one streamed and one single-shot document."""

    CSS = """
    .assistant-message { margin: 1 0; }
    .assistant-block { margin: 0; }
    .assistant-block--spaced { margin-top: 1; }
    """

    def __init__(self, single_source: str | None = None) -> None:
        super().__init__()
        self._single_source = single_source

    def compose(self) -> ComposeResult:
        if self._single_source is None:
            yield AssistantMarkdown("", classes="assistant-message")
        else:
            yield Static(RichMarkdown(self._single_source), classes="assistant-message")


async def _stream(
    document: AssistantMarkdown, source: str, pilot: Pilot[Any], step: int = 7
) -> None:
    """Feed *source* in fixed-size chunks like a token stream."""

    for index in range(0, len(source), step):
        document.set_source(source[: index + step])
        await pilot.pause()
    document.set_source(source)
    await pilot.pause()


def test_split_blocks_respects_fences() -> None:
    source = "para\n\n```py\nx = 1\n\ny = 2\n```\n\nafter"
    assert _split_blocks(source) == ["para", "```py\nx = 1\n\ny = 2\n```", "after"]


def test_split_blocks_handles_tilde_and_longer_closing_fence() -> None:
    source = "~~~\na\n\nb\n~~~~\n\ntail"
    assert _split_blocks(source) == ["~~~\na\n\nb\n~~~~", "tail"]


def test_split_blocks_keeps_unclosed_fence_open() -> None:
    assert _split_blocks("para\n\n```py\ncode\n\nmore") == ["para", "```py\ncode\n\nmore"]


def test_partition_blocks_keeps_last_block_open() -> None:
    frozen, tail = _partition_blocks(["a", "b", "c"])
    assert frozen == ["a", "b"]
    assert tail == "c"


def test_partition_blocks_delays_freeze_before_indented_successor() -> None:
    # "  continuation" may still merge into the list item above it (loose
    # list continuation), so the list item must not freeze yet.
    frozen, tail = _partition_blocks(["- item", "  continuation", "next"])
    assert frozen == []
    assert tail == "- item\n\n  continuation\n\nnext"


async def test_assistant_markdown_preserves_one_complete_source() -> None:
    source = (
        "A [reference][r].\n\n"
        "- loose item\n\n  continuation\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "[r]: https://example.com"
    )
    app = _DocApp()
    async with app.run_test() as pilot:
        document = app.query_one(AssistantMarkdown)
        await _stream(document, source, pilot)
        assert document.source == source

        await _stream(document, source + "\n\n```python\nopen fence", pilot)
        assert document.source.endswith("open fence")
        # The unclosed fence keeps its blank-line-free tail live; the blocks
        # before it stay frozen as separate children.
        assert len(document.children) > 1


async def test_split_rendering_matches_single_document_rendering() -> None:
    source = (
        "# Title\n\n"
        "A [reference][r] and `code`.\n\n"
        "- loose item\n\n  continuation\n\n"
        "```py\nx = 1\n\ny = 2\n```\n\n"
        "> quoted\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "para\n\n---\n\nafter rule\n\n"
        "[r]: https://example.com"
    )
    single = _DocApp(source)
    async with single.run_test(size=(80, 60)) as pilot:
        await pilot.pause()
        expected = "\n".join(
            strip.text for strip in single.screen._compositor.render_strips()  # type: ignore[reportPrivateUsage]
        )

    split = _DocApp()
    async with split.run_test(size=(80, 60)) as pilot:
        await _stream(split.query_one(AssistantMarkdown), source, pilot)
        actual = "\n".join(
            strip.text for strip in split.screen._compositor.render_strips()  # type: ignore[reportPrivateUsage]
        )

    assert actual == expected


async def test_late_link_definition_renders_frozen_reference_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed: list[str] = []
    real_markdown = streaming_markdown.RichMarkdown

    def counting(markup: str, *args: object, **kwargs: object) -> RichMarkdown:
        parsed.append(markup)
        return real_markdown(markup, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(streaming_markdown, "RichMarkdown", counting)

    app = _DocApp()
    async with app.run_test() as pilot:
        document = app.query_one(AssistantMarkdown)
        document.set_source("A [reference][r].\n\nnext")
        await pilot.pause()
        # "A [reference][r]." is now frozen, parsed without any definition.
        assert "A [reference][r]." in parsed

        document.set_source("A [reference][r].\n\nnext\n\n[r]: https://example.com")
        await pilot.pause()
        # The frozen block is re-rendered with the late definition appended.
        assert "A [reference][r].\n\n[r]: https://example.com" in parsed


async def test_streaming_parse_cost_scales_with_tail_not_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed_chars = 0
    real_markdown = streaming_markdown.RichMarkdown

    def counting(markup: str, *args: object, **kwargs: object) -> RichMarkdown:
        nonlocal parsed_chars
        parsed_chars += len(markup)
        return real_markdown(markup, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(streaming_markdown, "RichMarkdown", counting)

    blocks = [f"Paragraph {index} with some streaming text payload." for index in range(30)]
    app = _DocApp()
    async with app.run_test() as pilot:
        document = app.query_one(AssistantMarkdown)
        source = ""
        for block in blocks:
            source += block + "\n\n"
            document.set_source(source)
            await pilot.pause()

    # Re-parsing the whole document per frame costs O(n^2): the sum of every
    # streamed prefix, ~15x the final document here. Freezing closed blocks
    # keeps it near O(n): each block is parsed once when frozen and the open
    # tail is re-parsed per frame, well under 4x the final document.
    assert parsed_chars < 4 * len(source)


async def test_streaming_controller_renders_during_continuous_input() -> None:
    rendered: list[str] = []

    async def render(text: str) -> None:
        rendered.append(text)

    controller = StreamingMarkdownController(render, frame_interval=0.01)
    for token in "continuous stream":
        controller.append(token)
        await asyncio.sleep(0.002)

    assert rendered
    assert rendered[0] != "continuous stream"
    await controller.close()
    assert rendered[-1] == "continuous stream"


async def test_streaming_controller_serializes_updates_and_preserves_order() -> None:
    rendered: list[str] = []
    active = 0
    max_active = 0

    async def render(text: str) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.015)
        rendered.append(text)
        active -= 1

    controller = StreamingMarkdownController(render, frame_interval=0.005)
    for token in ("a", "b", "c", "d"):
        controller.append(token)
        await asyncio.sleep(0.007)

    await controller.flush()

    assert max_active == 1
    assert rendered[-1] == "abcd"
    assert rendered == sorted(rendered, key=len)
    assert controller.pending is False
    await controller.close()


async def test_streaming_controller_rejects_append_after_close() -> None:
    async def render(_text: str) -> None:
        return None

    controller = StreamingMarkdownController(render)
    await controller.close()

    with pytest.raises(RuntimeError, match="closed"):
        controller.append("late")


async def test_flush_cancels_frame_rescheduled_by_inflight_render() -> None:
    rendered: list[str] = []

    async def render(text: str) -> None:
        await asyncio.sleep(0.02)
        rendered.append(text)

    controller = StreamingMarkdownController(render, frame_interval=0.005)
    controller.append("a")
    await asyncio.sleep(0.01)
    controller.append("b")

    await controller.flush()

    assert rendered[-1] == "ab"
    assert controller.pending is False
    await controller.close()
