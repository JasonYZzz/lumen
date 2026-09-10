from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult

from lumen.tools.presentation import ToolPresentationCatalog
from lumen.ui.activity_indicator import (
    RunActivityIndicator,
    ToolActivityFamily,
    describe_tool_activity,
    tool_activity_presentation,
)
from lumen.ui.themes import register_themes


class ActivityHost(App[None]):
    def compose(self) -> ComposeResult:
        yield RunActivityIndicator()

    def on_mount(self) -> None:
        register_themes(self)  # type: ignore[arg-type]


def test_runtime_tool_cards_use_chinese_presentation_copy() -> None:
    catalog = ToolPresentationCatalog()

    search = catalog.call_view(
        "search_text",
        {"query": "async def run", "path": "src/lumen/agent_loop/loop.py"},
        origin="builtin",
        risk="read",
    )
    read = catalog.call_view("read_file", {"path": "README.md"}, origin="builtin", risk="read")

    assert search.active_verb == "正在搜索"
    assert search.completed_verb == "已搜索"
    assert read.active_verb == "正在读取"
    assert read.completed_verb == "已读取"


def test_tool_activity_descriptions_are_user_facing() -> None:
    assert describe_tool_activity("read_file", {"path": "src/app.py"}) == (
        "Reading",
        "src/app.py",
    )


def test_tool_activity_presentation_uses_semantic_families() -> None:
    read = tool_activity_presentation(
        "read_file", {"path": "README.md"}, origin="builtin", risk="read"
    )
    assert read.family is ToolActivityFamily.READ
    assert read.active_verb == "Reading"
    assert read.completed_verb == "Read"
    assert read.groupable is True

    search = tool_activity_presentation(
        "search_text",
        {"query": "outputs|默认|输出|deliverable|导出|报告", "path": "."},
        origin="builtin",
        risk="read",
    )
    assert search.family is ToolActivityFamily.SEARCH
    assert search.active_verb == "Searching"
    assert search.completed_verb == "Searched"
    assert search.detail == '“outputs | 默认 | 输出…” in workspace'

    command = tool_activity_presentation(
        "run_command", {"argv": ["rg", "needle", "."]}, origin="builtin", risk="read"
    )
    assert command.family is ToolActivityFamily.COMMAND
    assert command.groupable is False


def test_mcp_and_web_activity_keep_service_semantics() -> None:
    mcp = tool_activity_presentation(
        "list_issues", {}, origin="mcp:github", risk="read"
    )
    assert mcp.family is ToolActivityFamily.MCP
    assert mcp.active_verb == "Calling GitHub"
    assert mcp.completed_verb == "Called GitHub"
    assert mcp.group_key == "mcp:github"

    web = tool_activity_presentation(
        "web_search", {"query": "Textual accessibility"}, origin="mcp:web", risk="read"
    )
    assert web.family is ToolActivityFamily.WEB
    assert web.active_verb == "Searching the web"
    assert web.completed_verb == "Searched the web"
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


async def test_reduced_motion_uses_static_non_color_cue() -> None:
    app = ActivityHost()
    async with app.run_test() as pilot:
        indicator = app.query_one(RunActivityIndicator)
        indicator.set_animation_enabled(False)
        indicator.start("Thinking")
        await pilot.pause()

        assert str(indicator.content).startswith("• Thinking…")
