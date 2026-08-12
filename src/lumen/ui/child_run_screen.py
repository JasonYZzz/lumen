"""Live Agent panel backed by the Host's compatibility and Agent APIs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, ClassVar, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from lumen.ui.themes import theme_color


class AgentInputScreen(ModalScreen[str | None]):
    """Collect one bounded coordination message without complicating the Agent list."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    AgentInputScreen { align: center middle; background: $background 28%; }
    #agent-input-shell {
        width: 72%; max-width: 88; height: auto; padding: 1 2;
        background: $surface; border: tall $primary 70%;
    }
    #agent-input-title { height: 1; color: $text; text-style: bold; }
    #agent-input { height: 3; margin-top: 1; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-input-shell"):
            yield Static(self._prompt, id="agent-input-title", markup=False)
            yield Input(placeholder=self._prompt, id="agent-input")

    def on_mount(self) -> None:
        self.query_one("#agent-input", Input).focus()

    @on(Input.Submitted, "#agent-input")
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WorkStateScreen(ModalScreen[None]):
    """Inspect persistent work products and resolve pending verification effects."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("up", "previous", "Previous"),
        Binding("down", "next", "Next"),
        Binding("w", "waive", "Waive selected"),
        Binding("r", "refresh_now", "Refresh"),
    ]
    CSS = """
    WorkStateScreen { align: center middle; background: $background 28%; }
    #work-state-shell {
        width: 82%; max-width: 100; height: auto; max-height: 82%; padding: 1 2;
        background: $surface; border: tall $primary 70%;
    }
    #work-state-title { height: 1; color: $text; text-style: bold; }
    #work-state-list { height: auto; color: $text-muted; margin-top: 1; }
    #work-state-hint { height: 1; color: $text-muted; margin-top: 1; }
    """

    def __init__(
        self,
        provider: Callable[[], Awaitable[dict[str, list[dict[str, Any]]]]],
        waiver_provider: Callable[[str, str], Awaitable[dict[str, Any]]],
    ) -> None:
        super().__init__()
        self._provider = provider
        self._waiver_provider = waiver_provider
        self._state: dict[str, list[dict[str, Any]]] = {}
        self._selection = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="work-state-shell"):
            yield Static("Work state", id="work-state-title", markup=False)
            yield Static("Loading…", id="work-state-list", markup=False)
            yield Static(
                "↑↓ select pending effect · W record waiver · R refresh · Esc close",
                id="work-state-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.run_worker(self._load(), name="load-work-state", exclusive=True)

    async def _load(self) -> None:
        try:
            self._state = await self._provider()
            pending = self._state.get("pending_effects", [])
            self._selection = min(self._selection, max(0, len(pending) - 1))
            self._render_state()
        except Exception as error:
            self.query_one("#work-state-list", Static).update(f"Cannot load work state: {error}")

    def action_close(self) -> None:
        self.dismiss()

    def action_previous(self) -> None:
        pending = self._state.get("pending_effects", [])
        if pending:
            self._selection = (self._selection - 1) % len(pending)
            self._render_state()

    def action_next(self) -> None:
        pending = self._state.get("pending_effects", [])
        if pending:
            self._selection = (self._selection + 1) % len(pending)
            self._render_state()

    def action_refresh_now(self) -> None:
        self.run_worker(self._load(), name="refresh-work-state", exclusive=True)

    def action_waive(self) -> None:
        pending = self._state.get("pending_effects", [])
        if not pending:
            return
        effect_id = str(pending[self._selection].get("id", ""))
        if effect_id:
            app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
            app.push_screen(
                AgentInputScreen("Reason for verification waiver…"),
                lambda reason: self._submit_waiver(effect_id, reason),
            )

    def _submit_waiver(self, effect_id: str, raw_reason: str | None) -> None:
        reason = (raw_reason or "").strip()
        if reason:
            self.run_worker(
                self._waive(effect_id, reason),
                name=f"waive-effect-{effect_id}",
                exclusive=True,
            )

    async def _waive(self, effect_id: str, reason: str) -> None:
        try:
            await self._waiver_provider(effect_id, reason)
            self.notify(f"Waiver recorded for {effect_id}", timeout=2)
        except Exception as error:
            self.notify(f"Cannot waive {effect_id}: {error}", severity="error", timeout=4)
        await self._load()

    def _render_state(self) -> None:
        work_products = self._state.get("work_products", [])
        pending = self._state.get("pending_effects", [])
        recoverable = self._state.get("recoverable_effects", [])
        rows = [f"Work Products · {len(work_products)}"]
        rows.extend(
            f"  {item.get('resource', item.get('id', '?'))} · {item.get('status', 'unknown')}"
            for item in work_products
        )
        rows.append(f"\nPending Effects · {len(pending)}")
        rows.extend(
            f"{'>' if index == self._selection else ' '} {item.get('id', '?')} · "
            f"{item.get('operation', 'mutation')} · {item.get('status', 'pending')}"
            for index, item in enumerate(pending)
        )
        rows.append(f"\nRecoverable Effects · {len(recoverable)}")
        self.query_one("#work-state-list", Static).update("\n".join(rows))


class ChildRunScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("up", "previous", "Previous"),
        Binding("down", "next", "Next"),
        Binding("m", "message_agent", "Message"),
        Binding("f", "followup_agent", "Follow-up"),
        Binding("c", "cancel_child", "Interrupt"),
        Binding("i", "import_agent", "Import"),
        Binding("x", "reject_agent", "Reject import"),
        Binding("d", "close_agent", "Close"),
        Binding("w", "work_state", "Work state"),
        Binding("r", "refresh_now", "Refresh"),
    ]
    CSS = """
    ChildRunScreen { align: center middle; background: $background 28%; }
    #child-shell {
        width: 82%; max-width: 100; height: auto; max-height: 80%; padding: 1 2;
        background: $surface; border: tall $secondary 65%;
    }
    #child-title { height: 1; text-style: bold; color: $text; }
    #child-list { height: auto; color: $text-muted; margin-top: 1; }
    #child-hint { height: 1; color: $text-muted; margin-top: 1; }
    """

    def __init__(
        self,
        provider: Callable[[], Awaitable[list[dict[str, Any]]]],
        cancel_provider: Callable[[str], Awaitable[dict[str, Any]]],
        *,
        message_provider: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        followup_provider: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        import_provider: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        reject_provider: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        close_provider: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        work_state_provider: Callable[
            [], Awaitable[dict[str, list[dict[str, Any]]]]
        ] | None = None,
        waiver_provider: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._cancel_provider = cancel_provider
        self._message_provider = message_provider
        self._followup_provider = followup_provider
        self._import_provider = import_provider
        self._reject_provider = reject_provider
        self._close_provider = close_provider
        self._work_state_provider = work_state_provider
        self._waiver_provider = waiver_provider
        self._items: list[dict[str, Any]] = []
        self._selection = 0
        self._refreshing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="child-shell"):
            yield Static("Agents", id="child-title", markup=False)
            yield Static("Loading…", id="child-list", markup=False)
            yield Static(
                "↑↓ select · M message · F follow-up · C interrupt · I import · X reject · W work",
                id="child-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.set_interval(1.0, self._request_refresh)
        self._request_refresh()

    def _request_refresh(self) -> None:
        if not self._refreshing:
            self.run_worker(self._refresh_items(), name="refresh-child-runs", exclusive=False)

    async def _refresh_items(self) -> None:
        self._refreshing = True
        try:
            self._items = await self._provider()
            self._selection = min(self._selection, max(0, len(self._items) - 1))
            self._render_items()
        except Exception as error:
            self.query_one("#child-list", Static).update(f"Unable to load Agents: {error}")
        finally:
            self._refreshing = False

    def action_close(self) -> None:
        self.dismiss()

    def action_previous(self) -> None:
        if self._items:
            self._selection = (self._selection - 1) % len(self._items)
            self._render_items()

    def action_next(self) -> None:
        if self._items:
            self._selection = (self._selection + 1) % len(self._items)
            self._render_items()

    def action_refresh_now(self) -> None:
        self._request_refresh()

    def action_cancel_child(self) -> None:
        self._run_selected("interrupt", self._cancel_provider)

    def action_message_agent(self) -> None:
        if self._message_provider is not None:
            self._begin_input("message", "Message for selected Agent…")

    def action_followup_agent(self) -> None:
        if self._followup_provider is not None:
            self._begin_input("followup", "Follow-up task for selected Agent…")

    def action_import_agent(self) -> None:
        if self._import_provider is not None:
            self._run_selected("import", self._import_provider)

    def action_reject_agent(self) -> None:
        if self._reject_provider is not None:
            self._run_selected("reject", self._reject_provider)

    def action_close_agent(self) -> None:
        if self._close_provider is not None:
            self._begin_input("close", "Resolution for closing selected Agent…")

    def action_work_state(self) -> None:
        if self._work_state_provider is None or self._waiver_provider is None:
            return
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        app.push_screen(
            WorkStateScreen(self._work_state_provider, self._waiver_provider)
        )

    def _selected_id(self) -> str:
        if not self._items:
            return ""
        return str(self._items[self._selection].get("id", ""))

    def _run_selected(
        self,
        action: str,
        provider: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> None:
        agent_id = self._selected_id()
        if agent_id:
            self.run_worker(
                self._perform(action, agent_id, provider),
                name=f"agent-{action}-{agent_id}",
                exclusive=False,
            )

    def _begin_input(self, action: str, placeholder: str) -> None:
        if not self._selected_id():
            return
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        app.push_screen(
            AgentInputScreen(placeholder),
            lambda value: self._submit_input(action, value),
        )

    def _submit_input(self, action: str, raw_value: str | None) -> None:
        agent_id = self._selected_id()
        value = (raw_value or "").strip()
        if not agent_id or not value:
            return
        provider = {
            "message": self._message_provider,
            "followup": self._followup_provider,
            "close": self._close_provider,
        }[action]
        if provider is not None:
            self.run_worker(
                self._perform_with_value(action, agent_id, value, provider),
                name=f"agent-{action}-{agent_id}",
                exclusive=False,
            )

    async def _perform(
        self,
        action: str,
        agent_id: str,
        provider: Callable[[str], Awaitable[dict[str, Any]]],
    ) -> None:
        try:
            await provider(agent_id)
            self.notify(f"{action.title()} accepted for {agent_id}", timeout=2)
        except Exception as error:
            self.notify(f"Cannot {action} {agent_id}: {error}", severity="error", timeout=4)
        await self._refresh_items()

    async def _perform_with_value(
        self,
        action: str,
        agent_id: str,
        value: str,
        provider: Callable[[str, str], Awaitable[dict[str, Any]]],
    ) -> None:
        try:
            await provider(agent_id, value)
            self.notify(f"{action.title()} accepted for {agent_id}", timeout=2)
        except Exception as error:
            self.notify(f"Cannot {action} {agent_id}: {error}", severity="error", timeout=4)
        await self._refresh_items()

    def _render_items(self) -> None:
        if not self._items:
            self.query_one("#child-list", Static).update("No Agents for this session.")
            self.query_one("#child-title", Static).update("Agents · 0")
            return
        rendered = Text()
        for index, item in enumerate(self._items):
            marker = ">" if index == self._selection else " "
            status = str(item.get("status", "unknown"))
            kind = str(item.get("kind", "child"))
            owner = str(item.get("plan_step_id") or "session")
            duration = _duration(str(item.get("created_at", "")), str(item.get("updated_at", "")))
            task = " ".join(str(item.get("task", "")).split())
            if index:
                rendered.append("\n")
            status_glyph, status_token = _status_display(status)
            rendered.append(f"{marker} {item.get('id', '?')}  ")
            rendered.append(
                f"{status_glyph} {status}",
                style=self._theme_color(status_token, "#948A80"),
            )
            rendered.append(
                f"  {kind} · owner {owner} · {duration}\n"
                f"    {task[:120]}" + ("…" if len(task) > 120 else "")
            )
            worktree = item.get("worktree")
            branch = item.get("branch")
            commit = item.get("commit")
            if isinstance(worktree, str) and worktree:
                rendered.append("\n    worktree ")
                rendered.append(worktree, style=f"underline link file://{worktree}")
            if isinstance(branch, str) and branch:
                rendered.append(f" · branch {branch}", style="underline")
            if isinstance(commit, str) and commit:
                rendered.append(f" · commit {commit[:12]}", style="underline")
        active = sum(str(item.get("status")) in {"queued", "running"} for item in self._items)
        self.query_one("#child-title", Static).update(
            f"Agents · {len(self._items)} total · {active} active"
        )
        self.query_one("#child-list", Static).update(rendered)

    def _theme_color(self, token: str, fallback: str) -> str:
        app = cast(App[object], self.app)  # type: ignore[reportUnknownMemberType]
        return theme_color(app, token, fallback)


def _duration(start: str, end: str) -> str:
    try:
        seconds = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except ValueError:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _status_display(status: str) -> tuple[str, str]:
    if status in {"running", "executing"}:
        return "●", "tool"
    if status in {"queued", "waiting", "import_pending"}:
        return "○", "warning"
    if status in {"completed", "imported"}:
        return "✓", "success"
    if status in {"failed", "import_failed"}:
        return "✗", "error"
    if status in {"cancelled", "rejected"}:
        return "—", "activity-meta"
    return "·", "activity-meta"


__all__ = ["ChildRunScreen"]
