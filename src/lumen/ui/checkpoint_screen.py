"""Non-destructive session rewind over append-only checkpoints and receipts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class CheckpointScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("up", "previous", "Previous"),
        Binding("down", "next", "Next"),
        Binding("enter", "rewind", "Rewind"),
    ]
    CSS = """
    CheckpointScreen { align: center middle; background: $background 35%; }
    #checkpoint-shell {
        width: 82%; max-width: 100; height: auto; max-height: 82%; padding: 1 2;
        background: $surface; border: tall $primary 70%;
    }
    CheckpointScreen.confirming #checkpoint-shell { border: tall $warning; }
    #checkpoint-title { height: 1; text-style: bold; color: $text; }
    #checkpoint-list { height: auto; color: $text-muted; margin-top: 1; }
    #checkpoint-hint { height: 2; color: $text-muted; margin-top: 1; }
    """

    def __init__(
        self,
        provider: Callable[[], Awaitable[list[dict[str, Any]]]],
        fork_provider: Callable[[int], Awaitable[str]],
    ) -> None:
        super().__init__()
        self._provider = provider
        self._fork_provider = fork_provider
        self._items: list[dict[str, Any]] = []
        self._selection = 0
        self._confirming = False

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoint-shell"):
            yield Static("Session checkpoints", id="checkpoint-title", markup=False)
            yield Static("Loading…", id="checkpoint-list", markup=False)
            yield Static(
                "↑↓ select · Enter review/confirm · Esc close\n"
                "Rewind creates a new session branch; workspace files are not blindly reverted.",
                id="checkpoint-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self.run_worker(self._load(), name="load-checkpoints", exclusive=True)

    async def _load(self) -> None:
        try:
            self._items = await self._provider()
        except Exception as error:
            self.query_one("#checkpoint-list", Static).update(f"Cannot load checkpoints: {error}")
            return
        self._selection = max(0, len(self._items) - 1)
        self._render_items()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_previous(self) -> None:
        if self._items:
            self._selection = (self._selection - 1) % len(self._items)
            self._confirming = False
            self._render_items()

    def action_next(self) -> None:
        if self._items:
            self._selection = (self._selection + 1) % len(self._items)
            self._confirming = False
            self._render_items()

    def action_rewind(self) -> None:
        if not self._items:
            return
        if not self._confirming:
            self._confirming = True
            self.query_one("#checkpoint-hint", Static).update(
                "Enter again to create a new session at this checkpoint · Esc cancels\n"
                "Executor receipts are retained; current workspace files stay unchanged."
            )
            self._render_items()
            return
        turn_index = int(self._items[self._selection]["index"])
        self.run_worker(self._fork(turn_index), name="fork-checkpoint", exclusive=True)

    async def _fork(self, turn_index: int) -> None:
        try:
            session_id = await self._fork_provider(turn_index)
        except Exception as error:
            self.notify(f"Cannot rewind: {error}", severity="error", timeout=5)
            self._confirming = False
            return
        self.dismiss(session_id)

    def _render_items(self) -> None:
        self.set_class(self._confirming, "confirming")
        if not self._items:
            self.query_one("#checkpoint-list", Static).update("No completed turns yet.")
            return
        rows: list[str] = []
        for position, item in enumerate(self._items):
            marker = ">" if position == self._selection else " "
            receipt_count = int(item.get("receipt_count", 0))
            mutation_count = int(item.get("mutation_count", 0))
            prompt = " ".join(str(item.get("prompt", "")).split())
            rows.append(
                f"{marker} turn {int(item.get('index', position)) + 1} "
                f"[{item.get('status', 'unknown')}] · {receipt_count} receipts · "
                f"{mutation_count} side effects\n    {prompt[:120]}"
            )
        self.query_one("#checkpoint-title", Static).update(
            f"Session checkpoints · {len(self._items)}"
            + (" · confirm rewind" if self._confirming else "")
        )
        self.query_one("#checkpoint-list", Static).update("\n".join(rows))


__all__ = ["CheckpointScreen"]
