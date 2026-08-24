"""Semantic run activity indicator with a lightweight animated pulse."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import App
from textual.widgets import Static

from lumen.tools.presentation import ToolCallView
from lumen.ui.themes import FALLBACK_COLORS, theme_color


class ToolActivityFamily(StrEnum):
    """User-facing activity families; deliberately independent from execution risk."""

    READ = "read"
    LIST = "list"
    SEARCH = "search"
    WEB = "web"
    MCP = "mcp"
    COMMAND = "command"
    EDIT = "edit"
    PLAN = "plan"
    SKILL = "skill"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ToolActivityPresentation:
    """Semantic copy and grouping metadata for one visible tool activity."""

    family: ToolActivityFamily
    active_verb: str
    completed_verb: str
    detail: str | None
    groupable: bool = False
    group_key: str | None = None
    singular: str = "call"
    plural: str = "calls"
    tone: str = "tool"


class RunActivityIndicator(Static):
    """Show that a run is alive and describe its latest public activity."""

    DEFAULT_CSS = """
    RunActivityIndicator {
        display: none;
        height: 1;
        margin: 0 3;
        color: $text-muted;
    }
    RunActivityIndicator.running { display: block; }
    """

    _FRAMES: ClassVar[tuple[str, ...]] = ("✻", "✽", "✶", "✳", "✢", "✳")
    _STATIC_FRAME = "•"
    _FRAME_PHASES: ClassVar[tuple[str, ...]] = ("", "-shimmer", "", "-soft", "", "-shimmer")
    _TOOL_TONES: ClassVar[dict[str, str]] = {
        "read_file": "tool",
        "list_directory": "tool",
        "search_text": "tool",
        "run_command": "tool",
        "load_skill": "tool",
        "read_skill_resource": "tool",
        "write_file": "mode-edit",
        "edit_file": "mode-edit",
        "set_plan": "mode-plan",
        "update_step": "mode-plan",
    }

    def __init__(self) -> None:
        super().__init__("", id="activity-indicator", markup=False)
        self._started_at = 0.0
        self._label = "Thinking"
        self._detail: str | None = None
        self._tone = "activity"
        self._frame = 0
        self._tool_count = 0
        self._running = False
        self._animation_enabled = True

    def on_mount(self) -> None:
        self.set_interval(0.12, self._tick)

    @property
    def label(self) -> str:
        return self._label

    def start(self, label: str = "Thinking", detail: str | None = None) -> None:
        self._started_at = time.monotonic()
        self._label = label
        self._detail = detail
        self._tone = "activity"
        self._frame = 0
        self._tool_count = 0
        self._running = True
        self.add_class("running")
        self._render_line()

    def describe(self, label: str, detail: str | None = None, *, tone: str = "activity") -> None:
        if not self._running:
            return
        self._label = label
        self._detail = detail
        self._tone = tone
        self._render_line()

    def describe_tool(self, name: str, args: dict[str, Any]) -> None:
        self._tool_count += 1
        presentation = tool_activity_presentation(name, args)
        self.describe(
            presentation.active_verb,
            presentation.detail,
            tone=presentation.tone,
        )

    def stop(self) -> None:
        self._running = False
        self.remove_class("running")
        self.update("")

    def suspend(self, label: str, detail: str | None = None, *, tone: str = "activity") -> None:
        """Keep semantic phase text for audit/tests while hiding the fallback row."""

        self._label = label
        self._detail = detail
        self._tone = tone
        self._render_line()
        self._running = False
        self.remove_class("running")

    def set_animation_enabled(self, enabled: bool) -> None:
        """Enable animation, or freeze on the first frame for visual tests."""

        self._animation_enabled = enabled
        if not enabled:
            self._frame = 0
            if self._running:
                self._render_line()

    def _tick(self) -> None:
        if not self._running or not self._animation_enabled:
            return
        self._frame = (self._frame + 1) % len(self._FRAMES)
        self._render_line()

    def _render_line(self) -> None:
        elapsed = max(0, int(time.monotonic() - self._started_at))
        activity = self._theme_variable(self._tone, FALLBACK_COLORS["activity"])
        phase = self._FRAME_PHASES[self._frame]
        shimmer = self._theme_variable(f"{self._tone}{phase}", activity)
        detail_color = self._theme_variable(f"{self._tone}-detail", FALLBACK_COLORS["activity-detail"])
        meta_color = self._theme_variable("activity-meta", FALLBACK_COLORS["activity-meta"])

        # The glyph pulses through a narrow orange ramp while the verb stays
        # stable. This creates visible motion without making the whole line
        # flicker or compete with the assistant response above it.
        frame = self._FRAMES[self._frame] if self._animation_enabled else self._STATIC_FRAME
        line = Text(f"{frame} ", style=f"bold {shimmer}")
        label = self._label if self._label.endswith(("…", "?", ".")) else f"{self._label}…"
        line.append(label, style=f"bold {activity}")
        if self._detail:
            line.append(f"  {self._detail}", style=detail_color)
        meta = f"{elapsed}s"
        if self._tool_count:
            meta += f" · {self._tool_count} tool{'s' if self._tool_count != 1 else ''}"
        line.append(f"  ({meta})", style=meta_color)
        self.update(line)

    def _theme_variable(self, name: str, fallback: str) -> str:
        """Resolve a Rich-compatible color from the active Textual theme."""

        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        return theme_color(app, name, fallback)


def describe_tool_activity(name: str, args: dict[str, Any]) -> tuple[str, str | None]:
    """Translate a model-facing tool call into concise user-facing activity."""

    presentation = tool_activity_presentation(name, args)
    return presentation.active_verb, presentation.detail


def tool_activity_presentation(
    name: str,
    args: dict[str, Any],
    *,
    origin: str = "builtin",
    risk: str = "read",
    view: Mapping[str, Any] | None = None,
) -> ToolActivityPresentation:
    """Classify a tool by visible intent instead of treating every read risk alike."""

    if view is not None:
        intent = ToolCallView.model_validate(view)
        return ToolActivityPresentation(
            family=ToolActivityFamily(intent.family.value),
            active_verb=intent.active_verb,
            completed_verb=intent.completed_verb,
            detail=intent.detail,
            groupable=intent.groupable,
            group_key=intent.group_key,
            singular=intent.singular,
            plural=intent.plural,
            tone=intent.tone,
        )

    path = str(args.get("path", "")).strip() or None
    lowered = name.casefold()
    if _is_web_tool(lowered, origin):
        query = _compact_query(str(args.get("query", "")))
        return ToolActivityPresentation(
            ToolActivityFamily.WEB,
            "Searching the web",
            "Searched the web",
            query,
            groupable=True,
            group_key="web",
            singular="search",
            plural="searches",
        )
    if origin.startswith("mcp:"):
        service_key = origin.split(":", 1)[1].strip() or "external tool"
        service = _service_label(service_key)
        return ToolActivityPresentation(
            ToolActivityFamily.MCP,
            f"Calling {service}",
            f"Called {service}",
            _tool_label(name),
            groupable=risk == "read",
            group_key=f"mcp:{service_key.casefold()}",
        )
    if name == "read_file":
        return ToolActivityPresentation(
            ToolActivityFamily.READ,
            "Reading",
            "Read",
            _workspace_label(path),
            groupable=True,
            group_key="local-inspection",
            singular="file",
            plural="files",
        )
    if name == "list_directory":
        return ToolActivityPresentation(
            ToolActivityFamily.LIST,
            "Inspecting directory",
            "Inspected directory",
            _workspace_label(path),
            groupable=True,
            group_key="local-inspection",
            singular="directory",
            plural="directories",
        )
    if name == "search_text":
        query = str(args.get("query", "")).strip()
        target = _workspace_label(path)
        compact_query = _compact_query(query)
        detail = f"“{compact_query}” in {target}" if compact_query else target
        return ToolActivityPresentation(
            ToolActivityFamily.SEARCH,
            "Searching",
            "Searched",
            detail,
            groupable=True,
            group_key="local-search",
            singular="pattern",
            plural="patterns",
        )
    if name == "write_file":
        return ToolActivityPresentation(
            ToolActivityFamily.EDIT,
            "Writing",
            "Wrote",
            path,
            tone="mode-edit",
        )
    if name == "edit_file":
        return ToolActivityPresentation(
            ToolActivityFamily.EDIT,
            "Editing",
            "Edited",
            path,
            tone="mode-edit",
        )
    if name == "run_command":
        argv = args.get("argv")
        detail = (
            _one_line(" ".join(str(part) for part in cast(list[object], argv)), limit=72)
            if isinstance(argv, list)
            else None
        )
        return ToolActivityPresentation(
            ToolActivityFamily.COMMAND,
            "Running command",
            "Ran command",
            detail,
        )
    if name == "load_skill":
        return ToolActivityPresentation(
            ToolActivityFamily.SKILL,
            "Loading skill",
            "Loaded skill",
            str(args.get("name", "")).strip() or None,
        )
    if name == "read_skill_resource":
        skill = str(args.get("name", "")).strip()
        resource = str(args.get("path", "")).strip()
        detail = "/".join(part for part in (skill, resource) if part) or None
        return ToolActivityPresentation(
            ToolActivityFamily.SKILL,
            "Reading skill resource",
            "Read skill resource",
            detail,
        )
    if name == "set_plan":
        return ToolActivityPresentation(
            ToolActivityFamily.PLAN,
            "Planning",
            "Planned",
            None,
            tone="mode-plan",
        )
    if name == "update_step":
        return ToolActivityPresentation(
            ToolActivityFamily.PLAN,
            "Updating tasks",
            "Updated tasks",
            None,
            tone="mode-plan",
        )
    if name == "report_progress":
        return ToolActivityPresentation(
            ToolActivityFamily.PLAN,
            "Reviewing progress",
            "Reviewed progress",
            None,
            tone="mode-plan",
        )
    return ToolActivityPresentation(
        ToolActivityFamily.OTHER,
        "Using tool",
        "Used tool",
        _tool_label(name),
    )


def _is_web_tool(name: str, origin: str) -> bool:
    return origin.casefold() in {"mcp:web", "mcp:browser"} or any(
        token in name for token in ("web_search", "search_web", "web_fetch", "fetch_url")
    )


def _service_label(value: str) -> str:
    known = {"github": "GitHub", "gitlab": "GitLab", "slack": "Slack"}
    return known.get(value.casefold(), value.replace("_", " ").replace("-", " ").title())


def _tool_label(value: str) -> str:
    return value.replace("_", " ").strip()


def _workspace_label(path: str | None) -> str:
    return "workspace" if not path or path == "." else _one_line(path, limit=54)


def _compact_query(value: str) -> str:
    clean = " ".join(value.split())
    if not clean:
        return ""
    alternatives = [part.strip() for part in clean.split("|") if part.strip()]
    if len(alternatives) > 1:
        visible = " | ".join(alternatives[:3])
        return f"{visible}…" if len(alternatives) > 3 else visible
    return _one_line(clean, limit=42)


def _one_line(value: str, *, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


__all__ = [
    "RunActivityIndicator",
    "ToolActivityFamily",
    "ToolActivityPresentation",
    "describe_tool_activity",
    "tool_activity_presentation",
]
