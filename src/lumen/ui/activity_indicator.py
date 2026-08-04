"""Semantic run activity indicator with a lightweight animated pulse."""

from __future__ import annotations

import time
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.app import App
from textual.widgets import Static

from lumen.ui.themes import theme_color


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
        label, detail = describe_tool_activity(name, args)
        self.describe(label, detail, tone=self._TOOL_TONES.get(name, "tool"))

    def stop(self) -> None:
        self._running = False
        self.remove_class("running")
        self.update("")

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
        activity = self._theme_variable(self._tone, "#F0A24A")
        phase = self._FRAME_PHASES[self._frame]
        shimmer = self._theme_variable(f"{self._tone}{phase}", activity)
        detail_color = self._theme_variable(f"{self._tone}-detail", "#D8A56B")
        meta_color = self._theme_variable("activity-meta", "#948A80")

        # The glyph pulses through a narrow orange ramp while the verb stays
        # stable. This creates visible motion without making the whole line
        # flicker or compete with the assistant response above it.
        line = Text(f"{self._FRAMES[self._frame]} ", style=f"bold {shimmer}")
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

    path = str(args.get("path", "")).strip() or None
    if name == "read_file":
        return "Reading", path
    if name == "list_directory":
        return "Inspecting directory", path or "."
    if name == "search_text":
        query = str(args.get("query", "")).strip()
        detail = f"{query} in {path or '.'}" if query else path
        return "Searching", detail
    if name == "write_file":
        return "Writing", path
    if name == "edit_file":
        return "Editing", path
    if name == "run_command":
        argv = args.get("argv")
        detail = " ".join(str(part) for part in cast(list[object], argv)) if isinstance(argv, list) else None
        return "Running command", detail
    if name == "load_skill":
        return "Loading skill", str(args.get("name", "")).strip() or None
    if name == "read_skill_resource":
        skill = str(args.get("name", "")).strip()
        resource = str(args.get("path", "")).strip()
        detail = "/".join(part for part in (skill, resource) if part) or None
        return "Reading skill resource", detail
    if name == "set_plan":
        return "Planning", None
    if name == "update_step":
        return "Updating tasks", None
    if name == "report_progress":
        return "Reviewing progress", None
    return "Using tool", name.replace("_", " ")


__all__ = ["RunActivityIndicator", "describe_tool_activity"]
