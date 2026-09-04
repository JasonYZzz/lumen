"""Keyboard and pointer selection for model and permission-mode controls."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option


class ChoicePickerScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "previous", "Previous", show=False, priority=True),
        Binding("down", "next", "Next", show=False, priority=True),
    ]
    CSS = """
    ChoicePickerScreen { align: center middle; background: $background 35%; }
    #choice-picker-shell {
        width: 76; max-width: 94%; height: auto; max-height: 85%;
        padding: 1 2; background: $surface; border: round $primary 65%;
    }
    #choice-picker-title { height: 1; text-style: bold; color: $text; }
    #choice-picker-hint { height: auto; color: $text-muted; margin-top: 1; }
    #choice-picker-query { height: 3; margin-top: 1; }
    #choice-picker-options { height: auto; max-height: 12; border: none; }
    #choice-picker-empty { height: auto; color: $text-muted; display: none; }
    """

    def __init__(
        self, choices: list[tuple[str, str]], active: str, *, title: str,
        hint: str, read_only: bool = False,
    ) -> None:
        super().__init__()
        self._choices = choices
        self._title = title
        self._hint = hint
        self._active = active
        self._read_only = read_only

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-picker-shell"):
            yield Static(self._title, id="choice-picker-title", markup=False)
            yield Input(placeholder="Search…", id="choice-picker-query")
            yield OptionList(id="choice-picker-options")
            yield Static("No matching options", id="choice-picker-empty", markup=False)
            yield Static(
                self._hint,
                id="choice-picker-hint",
                markup=False,
            )

    def on_mount(self) -> None:
        self._filter("")
        self.query_one(Input).focus()

    @on(Input.Changed)
    def search(self, event: Input.Changed) -> None:
        self._filter(event.value)

    def _filter(self, query: str) -> None:
        query = query.strip().casefold()
        matches = [
            (name, model_id) for name, model_id in self._choices
            if query in f"{name} {model_id}".casefold()
        ]
        options = self.query_one(OptionList)
        options.clear_options()
        for name, model_id in matches:
            label = Text(f"{'✓' if name == self._active else ' '} {name}")
            label.append(f"\n  {model_id}", style="dim")
            options.add_option(Option(label, id=name, disabled=self._read_only))
        selected = next((i for i, (name, _) in enumerate(matches) if name == self._active), 0)
        options.highlighted = selected if matches else None
        self.query_one("#choice-picker-empty").display = not matches

    def action_next(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def action_previous(self) -> None:
        self.query_one(OptionList).action_cursor_up()

    @on(Input.Submitted)
    def submit(self) -> None:
        self.query_one(OptionList).action_select()

    @on(OptionList.OptionSelected)
    def choose(self, event: OptionList.OptionSelected) -> None:
        if not self._read_only:
            self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
