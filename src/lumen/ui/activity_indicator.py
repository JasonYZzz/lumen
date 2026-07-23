"""Semantic run activity indicator with a lightweight animated pulse."""

from __future__ import annotations

import time
from typing import Any, ClassVar, cast

from rich.text import Text
from textual.widgets import Static


class RunActivityIndicator(Static):
    """Show that a run is alive and describe its latest public activity."""

    DEFAULT_CSS = """
    RunActivityIndicator {
        display: none;
        height: 1;
        margin: 0 3;
        color: $accent;
    }
    RunActivityIndicator.running { display: block; }
    """

    _FRAMES: ClassVar[tuple[str, ...]] = ("✦", "✧", "✶", "✧")

    def __init__(self) -> None:
        super().__init__("", id="activity-indicator", markup=False)
        self._started_at = 0.0
        self._label = "Thinking"
        self._detail: str | None = None
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
        self._frame = 0
        self._tool_count = 0
        self._running = True
        self.add_class("running")
        self._render_line()

    def describe(self, label: str, detail: str | None = None) -> None:
        if not self._running:
            return
        self._label = label
        self._detail = detail
        self._render_line()

    def describe_tool(self, name: str, args: dict[str, Any]) -> None:
        self._tool_count += 1
        label, detail = describe_tool_activity(name, args)
        self.describe(label, detail)

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
        line = Text(f"{self._FRAMES[self._frame]} ")
        line.append(self._label, style="bold")
        if self._detail:
            line.append(f"  {self._detail}", style="dim")
        meta = f"{elapsed}s"
        if self._tool_count:
            meta += f" · {self._tool_count} tool{'s' if self._tool_count != 1 else ''}"
        line.append(f"  ({meta})", style="dim")
        self.update(line)


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
        return "Building todo list", None
    if name == "update_step":
        return "Updating todo list", None
    if name == "report_progress":
        return "Reviewing progress", None
    return "Using tool", name.replace("_", " ")


__all__ = ["RunActivityIndicator", "describe_tool_activity"]
