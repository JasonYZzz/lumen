"""Topbar / welcome panel / status line rendering, extracted from ``app.py``.

``StatusBarMixin`` builds every persistent chrome string: the topbar, the
welcome-panel context, and the composed status line (mode badge + run state +
usage + keymap hints). ``LumenApp`` is only imported under ``TYPE_CHECKING``
to avoid a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual.app import App
from textual.widgets import Static

from lumen.approval import ApprovalMode
from lumen.branding import product_label
from lumen.events import UsageUpdated
from lumen.ui.themes import theme_color
from lumen.ui.welcome import WelcomePanel

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


def _fmt_tokens(n: int) -> str:
    """Compact token count: 1234 → ``1.2k``, 567 → ``567``."""

    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class StatusBarMixin:
    """Render topbar, welcome panel, and the composed status line."""

    def _refresh_topbar(self: LumenApp) -> None:
        session_id = self.session.id[:8] if self.session else "none"
        # MCP status: only shown when there are servers AND some are unhealthy.
        # A fully-healthy or zero-MCP config stays silent to reduce noise.
        mcp_segment = self._mcp_warning_segment()
        topbar = (
            f"◆ {product_label(self.config.agent.name)}  ·  {self.resources.workspace.name}  ·  {session_id}"
        )
        if mcp_segment:
            topbar += f"  ·  {mcp_segment}"
        self.query_one("#topbar", Static).update(topbar)
        self._refresh_welcome_panel()

    def _model_display(self: LumenApp) -> str:
        model_id = self.resources.active_model_config().id
        available = self.resources.available_models()
        if len(available) <= 1:
            return model_id
        active_name = self.resources.active_model_name()
        index = available.index(active_name) + 1
        return f"{active_name} [{model_id}] {index}/{len(available)}"

    def _refresh_welcome_panel(self: LumenApp) -> None:
        try:
            welcome = self.query_one("#welcome", WelcomePanel)
        except Exception:
            return
        statuses = self.resources.mcp_status
        if statuses:
            ready = sum(status == "ok" for status in statuses.values())
            mcp_summary = f"MCP {ready}/{len(statuses)} ready"
        else:
            mcp_summary = "no MCP servers"
        source_scopes: list[str] = []
        for source in self.config.config_sources:
            source_scopes.append(str(getattr(source, "scope", "explicit")))
        config_summary = ", ".join(source_scopes) or "explicit"
        welcome.update_context(
            agent_name=self.config.agent.name,
            model=self._model_display(),
            mode=self._approval_mode.value,
            session_id=self.session.id[:8] if self.session is not None else "starting",
            workspace=self.resources.workspace,
            tool_count=len(self.resources.tool_metadata),
            skill_count=len(self.resources.skills),
            mcp_summary=mcp_summary,
            config_summary=config_summary,
            project_trusted=self.config.project_trusted,
            session_directory=self.config.sessions.directory,
        )

    def _mcp_warning_segment(self: LumenApp) -> str:
        """MCP status text, shown only when there's a problem.

        Returns ``""`` when MCP is healthy or unconfigured, so the topbar
        stays clean in the common case. When some servers failed, we surface
        a compact warning.
        """

        statuses = self.resources.mcp_status
        if not statuses:
            return ""
        ok = sum(1 for value in statuses.values() if value == "ok")
        if ok == len(statuses):
            return ""  # all healthy — no noise
        return f"MCP {ok}/{len(statuses)}"

    def _mcp_summary(self: LumenApp) -> str:
        statuses = self.resources.mcp_status
        if not statuses:
            return "0"
        ok = sum(1 for value in statuses.values() if value == "ok")
        return f"{ok}/{len(statuses)}"

    def _status_suffix(self: LumenApp) -> str:
        """Persistent runtime context and keymap hints.

        Kept on every status update so the user always sees which model and
        approval mode are active, plus the most important keymap hints. The
        ``│`` separates the config segment from the keymap segment, and ``·``
        separates items within each segment.
        """

        width = self.size.width if self.is_running else 120
        parts = [self._model_display()]
        if width >= 120:
            parts.append("/ commands · @ files · Alt+C copy")
        elif width >= 80:
            parts.append("/ · @")
        return "  │  " + "  │  ".join(parts)

    def _status(self: LumenApp, state: str) -> Text:
        """Build a full status line: ``<state><suffix>``.

        ``state`` is the run-state text ("Thinking…", "Ready", the usage
        summary). We always append the model + keymap suffix so it
        never disappears during a run.
        """

        mode_text, mode_token = {
            ApprovalMode.MANUAL: ("⏸ manual mode on", "mode-manual"),
            ApprovalMode.ACCEPT_EDITS: ("⏵⏵ accept edits on", "mode-edit"),
            ApprovalMode.PLAN: ("⏸ plan mode on", "mode-plan"),
            ApprovalMode.AUTO: ("⏵⏵ auto mode on", "mode-auto"),
        }[self._approval_mode]
        routine_state = (
            state in {"Ready", "Thinking…"}
            or state.startswith("Running ")
            or state.startswith("Approval required:")
        )
        usage = self._usage_summary()
        app = cast(App[object], self)
        mode_color = theme_color(app, mode_token, "#948A80")
        meta_color = theme_color(app, "activity-meta", "#948A80")
        rendered = Text()
        rendered.append(mode_text, style=f"bold {mode_color}")
        rendered.append(" (shift+tab to cycle)", style=meta_color)
        if not routine_state:
            state_token = (
                "error"
                if state in {"Denied", "Run failed", "Startup error"}
                else "warning"
                if state.startswith(("Waiting", "Approval required"))
                else "foreground"
            )
            state_color = theme_color(app, state_token, "#ECE9E4")
            rendered.append(" · ", style=meta_color)
            rendered.append(state, style=state_color)
        if usage and self.size.width >= 80:
            rendered.append(f" · {usage}", style=meta_color)
        rendered.append(self._status_suffix(), style=meta_color)
        return rendered

    def _usage_summary(self: LumenApp) -> str:
        event = self._last_usage_event
        if event is None:
            return ""
        usage = event.usage or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        return (
            f"ctx {_fmt_tokens(event.context_tokens_estimate)} · "
            f"req {event.request_count} · tools {event.tool_call_count} · "
            f"{_fmt_tokens(input_tokens)}/{_fmt_tokens(output_tokens)} tok · "
            f"{event.elapsed_seconds:.1f}s"
        )

    def _status_line(self: LumenApp, event: UsageUpdated) -> Text:
        self._last_usage_event = event
        return self._status("Ready")

    def _refresh_mode_classes(self: LumenApp, status: Static) -> None:
        status.set_class(self._approval_mode is ApprovalMode.ACCEPT_EDITS, "mode-accept")
        status.set_class(self._approval_mode is ApprovalMode.PLAN, "mode-plan")
        status.set_class(self._approval_mode is ApprovalMode.AUTO, "mode-auto")
