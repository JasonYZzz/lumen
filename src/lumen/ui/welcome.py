"""Composed idle-state context for the TUI timeline."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from rich.text import Text
from textual.app import App
from textual.widgets import Static

from lumen.branding import product_label
from lumen.ui.themes import theme_color


class WelcomePanel(Static):
    """Show the runtime context users need before their first prompt."""

    def __init__(self) -> None:
        super().__init__("Starting workspace…", id="welcome", classes="welcome-panel", markup=False)

    def update_context(
        self,
        *,
        agent_name: str,
        model: str,
        mode: str,
        session_id: str,
        workspace: Path,
        tool_count: int,
        skill_count: int,
        mcp_summary: str,
        config_summary: str | None = None,
        project_trusted: bool = True,
        session_directory: Path | None = None,
    ) -> None:
        mode_detail = {
            "manual": "confirm risky tools",
            "accept_edits": "edit files automatically",
            "plan": "read-only exploration",
            "auto": "classified tools run automatically",
        }.get(mode, "confirm risky tools")
        session_detail = f" · {session_directory}" if session_directory is not None else ""
        project_detail = (
            f" · {'trusted' if project_trusted else 'untrusted'} · config {config_summary}"
            if config_summary is not None
            else ""
        )
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        primary = theme_color(app, "primary", "#C99552")
        success = theme_color(app, "success", "#86A66C")
        foreground = theme_color(app, "foreground", "#ECE9E4")
        muted = theme_color(app, "activity-meta", "#948A80")
        tool = theme_color(app, "tool", "#C7ACE8")
        mode_token = {
            "accept_edits": "mode-edit",
            "plan": "mode-plan",
            "auto": "mode-auto",
        }.get(mode, "mode-manual")
        mode_color = theme_color(app, mode_token, muted)

        rendered = Text()
        rendered.append(product_label(agent_name), style=f"bold {primary}")
        rendered.append("\nWorkspace ready", style=success)
        rendered.append("\n\n")

        def append_field(label: str, value: str, color: str = foreground) -> None:
            rendered.append(f"{label:<11}", style=f"bold {muted}")
            rendered.append(value, style=color)
            rendered.append("\n")

        append_field("Model", model)
        append_field("Mode", f"{mode} · {mode_detail}", mode_color)
        append_field("Session", f"{session_id}{session_detail}")
        append_field("Project", f"{workspace}{project_detail}")
        append_field(
            "Outputs",
            "outputs/ · default generated deliverables",
            theme_color(app, "mode-edit", "#69B9AF"),
        )
        append_field("Resources", f"{tool_count} tools · {skill_count} skills · {mcp_summary}", tool)
        rendered.append("\nType a request", style=foreground)
        rendered.append(" · / commands", style=primary)
        rendered.append(" · @ files", style=theme_color(app, "mode-edit", "#69B9AF"))
        rendered.append(" · Shift+Tab cycles mode", style=muted)
        self.update(rendered)


__all__ = ["WelcomePanel"]
