"""Command palette providers for the Lumen TUI.

This is the Lumen counterpart to posting's ``commands.py``: we lean on
Textual's native ``CommandPalette`` + ``Provider`` API (bound to ``Ctrl+P``)
rather than building a bespoke overlay. The palette gives users fuzzy search
over every command for free, replacing the old ``/help``-only discoverability.

Each command is a ``(name, callback, help_text)`` tuple. The callback is an
``app.action_*`` method (or a ``partial`` of one) so the palette can invoke it
directly without us plumbing a string-protocol.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from textual.command import DiscoveryHit, Hit, Hits, Provider

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


# A command tuple: (display name, runnable callback, help text).
Command = tuple[str, Any, str]


class LumenCommandProvider(Provider):
    """Contribute Lumen's commands to the Textual command palette.

    The palette is opened with ``Ctrl+P``; the user types to fuzzy-filter,
    then ``Enter`` runs the highlighted command. Discovery hits (shown when
    the palette is empty) are the highest-value commands.
    """

    @property
    def lumen_app(self) -> LumenApp:
        """The owning LumenApp, typed for our command callbacks."""

        # pyright can't narrow App[Unknown] from screen.app, so we annotate.
        app: LumenApp = self.screen.app  # type: ignore[assignment]
        return app

    @property
    def commands(self) -> tuple[Command, ...]:
        """Build the live command list based on current app state.

        Re-evaluated on every palette open so commands like "switch to model
        X" reflect the currently-configured models, not a stale snapshot.
        """

        app = self.lumen_app
        out: list[Command] = []

        # --- session management -------------------------------------------
        out.append(("session: New", app.action_new_session, "Start a fresh session"))
        out.append(("session: List recent", app.action_list_sessions, "Show recent session UUIDs"))
        out.append(
            (
                "session: Resume…",
                app.action_prompt_resume,
                "Resume a session by UUID (you'll be asked for the id)",
            )
        )

        # --- model switching ----------------------------------------------
        # One command per configured model so the user can pick by name.
        for model_name in app.resources.available_models():
            out.append(
                (
                    f"model: Switch to {model_name}",
                    partial(app.action_switch_model, model_name),
                    f"Switch the active model to {model_name}",
                )
            )

        # --- tools / state inspection -------------------------------------
        out.append(("tools: List visible", app.action_list_tools, "Show model-visible tools"))
        out.append(("view: Show prompt history", app.action_show_history, "Browse recent prompts"))
        out.append(
            (
                "view: Clear timeline",
                app.action_clear_timeline,
                "Clear visible activity without resetting session context",
            )
        )

        # --- conversation control -----------------------------------------
        out.append(("run: Retry last prompt", app.action_retry_last, "Re-send the previous prompt"))

        # --- approval mode ------------------------------------------------
        # One command per mode so the user can see the current state and switch
        # directly. The label reflects the target mode (not the current one)
        # so the user picks what they want, not what they have.
        out.append(
            (
                "approval: Switch to manual mode",
                partial(app.request_approval_mode, "manual"),
                "Confirm every approval-gated tool call",
            )
        )
        out.append(
            (
                "approval: Switch to accept-edits mode",
                partial(app.request_approval_mode, "accept_edits"),
                "Auto-approve builtin file writes and edits only",
            )
        )
        out.append(
            (
                "approval: Switch to auto mode",
                partial(app.request_approval_mode, "auto"),
                "Auto-approve classified tools; still confirm unknown remote tools",
            )
        )

        # --- app ----------------------------------------------------------
        out.append(("app: Quit", app.action_safe_quit, "Cancel the active run cleanly, then exit"))

        return tuple(out)

    async def discover(self) -> Hits:
        """Yield the always-visible discovery hits (shown when palette empty)."""

        # Surface the most useful starting points so an empty palette isn't
        # a blank wall.
        priority = {
            "session: New",
            "session: Resume…",
            "tools: List visible",
            "app: Quit",
        }
        for name, runnable, help_text in self.commands:
            if name in priority:
                yield DiscoveryHit(name, runnable, help=help_text)

    async def search(self, query: str) -> Hits:
        """Yield fuzzy-matched commands for ``query``."""

        matcher = self.matcher(query)
        for name, runnable, help_text in self.commands:
            if (score := matcher.match(name)) > 0:
                yield Hit(score, matcher.highlight(name), runnable, help=help_text)


__all__ = ["LumenCommandProvider"]
