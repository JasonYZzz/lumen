from __future__ import annotations

import asyncio

import pytest

from lumen.ui.streaming_markdown import AssistantMarkdown, StreamingMarkdownController


def test_assistant_markdown_preserves_one_complete_source() -> None:
    source = (
        "A [reference][r].\n\n"
        "- loose item\n\n  continuation\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "[r]: https://example.com"
    )
    document = AssistantMarkdown(source)

    assert document.source == source
    assert not document.children

    document.set_source(source + "\n\n```python\nopen fence")
    assert document.source.endswith("open fence")


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
