"""Non-destructive session rewind over append-only checkpoints and receipts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar
from uuid import uuid4

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
        Binding("f", "preview_files", "Preview files"),
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
        *,
        preview_provider: Callable[[int], Awaitable[dict[str, Any]]] | None = None,
        restore_provider: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._fork_provider = fork_provider
        self._preview_provider = preview_provider
        self._restore_provider = restore_provider
        self._restore_preview: dict[str, Any] | None = None
        self._restore_request_id = ""
        self._items: list[dict[str, Any]] = []
        self._selection = 0
        self._confirming = False

    def compose(self) -> ComposeResult:
        with Vertical(id="checkpoint-shell"):
            yield Static("Session checkpoints", id="checkpoint-title", markup=False)
            yield Static("Loading…", id="checkpoint-list", markup=False)
            yield Static(
                "↑↓ select · Enter branch · F preview files · Esc close\n"
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
            self._restore_preview = None
            self._render_items()

    def action_next(self) -> None:
        if self._items:
            self._selection = (self._selection + 1) % len(self._items)
            self._confirming = False
            self._restore_preview = None
            self._render_items()

    def action_rewind(self) -> None:
        if self._restore_preview is not None:
            self.run_worker(self._restore_files(), name="restore-files", exclusive=True)
            return
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

    def action_preview_files(self) -> None:
        if self._items and self._preview_provider is not None:
            self.run_worker(self._preview_files(), name="preview-files", exclusive=True)

    async def _preview_files(self) -> None:
        if self._preview_provider is None:
            return
        try:
            selection = self._selection
            result = await self._preview_provider(int(self._items[selection]["index"]))
            if selection != self._selection:
                return
            self._restore_preview = result
            self._restore_request_id = str(uuid4())
            preview = self._restore_preview
            rows = [f"Restore files before turn {preview['before_turn'] + 1}"]
            rows.extend(f"\n{item['resource']}\n{item['diff']}" for item in preview["files"])
            rows.extend(f"Conflict: {item['resource']} · {item['error']}" for item in preview["blockers"])
            rows.append(f"Excluded operations: {len(preview['excluded'])}; conversation stays current.")
            self.query_one("#checkpoint-list", Static).update("\n".join(rows))
            self.query_one("#checkpoint-hint", Static).update(
                "Enter explicitly confirms these files · ↑↓ cancels preview · Esc closes"
            )
        except Exception as error:
            self.notify(f"Cannot preview file restore: {error}", severity="error", timeout=5)

    async def _restore_files(self) -> None:
        preview = self._restore_preview
        if preview is None or self._restore_provider is None:
            return
        if preview["blockers"] or not preview["files"]:
            self.notify("Resolve conflicts and preview at least one file first", severity="warning")
            return
        try:
            result = await self._restore_provider(preview, self._restore_request_id)
            if result.get("failed"):
                failure = result["failed"]
                self.notify(f"Partial restore: {failure['resource']} · {failure['error']}", severity="error")
                return
            self.notify(f"File restore: {result['status']}", timeout=5)
            self._restore_preview = None
            await self._load()
        except Exception as error:
            self.notify(f"File restore failed: {error}", severity="error", timeout=5)

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
        if not self._confirming and self._restore_preview is None:
            self.query_one("#checkpoint-hint", Static).update(
                "↑↓ select · Enter branch · F preview files · Esc close\n"
                "File restore requires a separate preview and confirmation."
            )
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
