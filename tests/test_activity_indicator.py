from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult

from lumen.ui.activity_indicator import RunActivityIndicator, describe_tool_activity
from lumen.ui.themes import register_themes


class ActivityHost(App[None]):
    def compose(self) -> ComposeResult:
        yield RunActivityIndicator()

    def on_mount(self) -> None:
        register_themes(self)  # type: ignore[arg-type]


def test_tool_activity_descriptions_are_user_facing() -> None:
    assert describe_tool_activity("read_file", {"path": "src/app.py"}) == (
        "Reading",
        "src/app.py",
    )
    assert describe_tool_activity("write_file", {"path": "outputs/report.md"}) == (
        "Writing",
        "outputs/report.md",
    )
    assert describe_tool_activity("run_command", {"argv": ["uv", "run", "pytest"]}) == (
        "Running command",
        "uv run pytest",
    )
    assert describe_tool_activity("read_skill_resource", {"name": "arp-report", "path": "reference.md"}) == (
        "Reading skill resource",
        "arp-report/reference.md",
    )


async def test_activity_indicator_animates_and_stops() -> None:
    app = ActivityHost()
    async with app.run_test() as pilot:
        indicator = app.query_one(RunActivityIndicator)
        indicator.start("Thinking", "understanding the request")
        await pilot.pause()
        before = str(indicator.content)
        assert indicator.has_class("running")
        assert "Thinking" in before

        rendered = indicator.content
        assert isinstance(rendered, Text)
        styles = {str(span.style) for span in rendered.spans}
        assert "bold #F0A24A" in styles
        assert "#D8A56B" in styles
        assert "#948A80" in styles

        indicator.describe_tool("read_file", {"path": "README.md"})
        await asyncio.sleep(0.13)
        await pilot.pause()
        after = str(indicator.content)
        assert "Reading" in after and "README.md" in after
        assert after != before
        rendered = indicator.content
        assert isinstance(rendered, Text)
        tool_styles = {str(span.style) for span in rendered.spans}
        assert "bold #C7ACE8" in tool_styles
        assert "#BCA8D1" in tool_styles

        indicator.describe_tool("edit_file", {"path": "src/app.py"})
        edit_rendered = indicator.content
        assert isinstance(edit_rendered, Text)
        edit_styles = {str(span.style) for span in edit_rendered.spans}
        assert "bold #69B9AF" in edit_styles
        assert "#8FBFB9" in edit_styles

        indicator.stop()
        assert not indicator.has_class("running")
