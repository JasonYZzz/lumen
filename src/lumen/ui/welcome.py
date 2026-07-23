"""Composed idle-state context for the TUI timeline."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Static

from lumen.branding import product_label


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
    ) -> None:
        mode_detail = "reads run without prompts" if mode == "auto" else "confirm risky tools"
        self.update(
            "\n".join(
                [
                    product_label(agent_name),
                    "Workspace ready",
                    "",
                    f"Model      {model}",
                    f"Mode       {mode} · {mode_detail}",
                    f"Session    {session_id}",
                    f"Project    {workspace}",
                    "Outputs    outputs/ · default generated deliverables",
                    f"Resources  {tool_count} tools · {skill_count} skills · {mcp_summary}",
                    "",
                    "Type a request · / commands · @ files · Shift+Tab mode · Ctrl+P palette",
                ]
            )
        )


__all__ = ["WelcomePanel"]
