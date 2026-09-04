"""Branded zero-state shown before a session produces timeline content."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from rich.text import Text
from textual.app import App
from textual.widgets import Static

from lumen.ui.themes import FALLBACK_COLORS, theme_color

_WORDMARK_TEXT = "lumen"
_MARK_GLYPH = (
    "0111000",
    "1111000",
    "1111000",
    "1111000",
    "1100000",
    "1101110",
    "0111110",
)
_WORDMARK_GLYPHS = {
    "l": (
        "11000",
        "01000",
        "01000",
        "01000",
        "01000",
        "01000",
        "01110",
    ),
    "u": (
        "00000",
        "00000",
        "10001",
        "10001",
        "10001",
        "10011",
        "01110",
    ),
    "m": (
        "00000",
        "00000",
        "11010",
        "10101",
        "10101",
        "10101",
        "10101",
    ),
    "e": (
        "00000",
        "00000",
        "01110",
        "10001",
        "11111",
        "10000",
        "01111",
    ),
    "n": (
        "00000",
        "00000",
        "11110",
        "10001",
        "10001",
        "10001",
        "10001",
    ),
}


def _wordmark_rows() -> tuple[str, ...]:
    """Compose the Lumen Fold mark and lowercase wordmark for terminals."""

    return tuple(
        f"{_MARK_GLYPH[row]}  "
        + " ".join(_WORDMARK_GLYPHS[letter][row] for letter in _WORDMARK_TEXT)
        for row in range(7)
    )


def _render_wordmark(*, highlight: str, primary: str) -> Text:
    """Render square terminal pixels using paired upper/lower half blocks."""

    wordmark_rows = _wordmark_rows()
    rows = (*wordmark_rows, " " * len(wordmark_rows[0]))
    rendered = Text()
    for row in range(0, len(rows), 2):
        for upper, lower in zip(rows[row], rows[row + 1], strict=True):
            if upper == "1" and lower == "1":
                rendered.append("▀", style=f"{highlight} on {primary}")
            elif upper == "1":
                rendered.append("▀", style=highlight)
            elif lower == "1":
                rendered.append("▄", style=primary)
            else:
                rendered.append(" ")
        if row + 2 < len(rows):
            rendered.append("\n")
    return rendered


def _render_zero_state(
    *,
    primary: str,
    highlight: str,
    soft: str,
    success: str,
    foreground: str,
    muted: str,
    tool: str,
    edit: str,
    tool_count: int,
    skill_count: int,
    mcp_summary: str,
) -> Text:
    """Render the amber wordmark and compact startup guidance."""

    display_mcp_summary = "no MCP" if mcp_summary == "no MCP servers" else mcp_summary
    rendered = _render_wordmark(highlight=highlight, primary=primary)
    rendered.append("\n")
    rendered.append("╶──────────── ◆ ────────────╴", style=soft)
    rendered.append("\n\n")
    rendered.append("Workspace ready", style=f"bold {success}")
    rendered.append("\n")
    rendered.append(
        f"{tool_count} tools · {skill_count} skills · {display_mcp_summary}",
        style=tool,
    )
    rendered.append("\n\n")
    rendered.append("/", style=f"bold {primary}")
    rendered.append(" commands", style=foreground)
    rendered.append("  ·  ", style=muted)
    rendered.append("@", style=f"bold {edit}")
    rendered.append(" files", style=foreground)
    rendered.append("\n")
    rendered.append("Ctrl+P command palette", style=muted)
    return rendered


class WelcomePanel(Static):
    """Show a branded zero-state until real timeline content is mounted."""

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
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]

        # These values already live in the persistent top/status bars.
        del agent_name, model, mode, session_id, workspace
        del config_summary, project_trusted, session_directory
        self.update(
            _render_zero_state(
                primary=theme_color(app, "primary", FALLBACK_COLORS["primary"]),
                highlight=theme_color(app, "activity-shimmer", FALLBACK_COLORS["activity-shimmer"]),
                soft=theme_color(app, "activity-soft", FALLBACK_COLORS["activity-soft"]),
                success=theme_color(app, "success", FALLBACK_COLORS["success"]),
                foreground=theme_color(app, "foreground", FALLBACK_COLORS["foreground"]),
                muted=theme_color(app, "activity-meta", FALLBACK_COLORS["activity-meta"]),
                tool=theme_color(app, "tool", FALLBACK_COLORS["tool"]),
                edit=theme_color(app, "mode-edit", FALLBACK_COLORS["mode-edit"]),
                tool_count=tool_count,
                skill_count=skill_count,
                mcp_summary=mcp_summary,
            )
        )


__all__ = ["WelcomePanel"]
