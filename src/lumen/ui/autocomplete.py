"""Completion dropdown widget for the prompt editor.

A thin wrapper around ``OptionList`` that adds the two bindings OptionList
lacks: ``Tab`` to accept the highlighted suggestion and ``Esc`` to dismiss.
The dropdown is keyboard-driven only (no mouse), matching the rest of the
TUI and pi/tui's design.

Key design note: this widget is *not* meant to receive focus. The parent
``PromptEditor`` keeps focus so the user can keep typing while the dropdown
is open. The editor forwards Up/Down/Enter/Tab/Esc to the dropdown via
``action_*`` calls; this widget only owns the bindings so the keys are
interpreted correctly when they arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from textual.widget import Widget


@dataclass(frozen=True, slots=True)
class CompletionSuggestion:
    """One item in the completion dropdown.

    ``label`` is what the user sees; ``insert`` is what replaces the trigger
    prefix in the editor when picked. They can differ — e.g. a file
    suggestion's label might be ``config.yaml`` while ``insert`` is
    ``@config.yaml`` (preserving the trigger character the user typed).
    """

    label: str
    insert: str
    description: str | None = None


class CompletionDropdown(OptionList):
    """Inline completion dropdown shown below the prompt editor.

    Add suggestions via :meth:`set_suggestions`. Listen for
    :class:`SuggestionSelected` and :class:`Dismissed` to drive the editor.
    """

    DEFAULT_CSS = """
    CompletionDropdown {
        /* Float above all content. ``dock: top`` anchors the widget's
           natural position to the top of the screen so ``anchor_above`` can
           compute a simple, predictable downward offset. ``layer: above``
           ensures the dropdown paints over the message timeline beneath it.
           We previously used ``dock: bottom`` which pinned the dropdown to
           the screen bottom and covered the prompt on short terminals. */
        layer: above;
        dock: top;
        max-height: 10;
        min-width: 48;
        max-width: 110;
        width: 92%;
        height: auto;
        display: none;
        background: $panel 98%;
        border-top: solid $primary 45%;
        border-bottom: solid $primary 45%;
        padding: 0;
        margin: 0;
    }
    CompletionDropdown > .option-list--option-highlighted {
        background: $primary 30%;
        color: $text;
        text-style: bold;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "select", "Accept", show=False),
        Binding("escape", "dismiss", "Close", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="completion-dropdown")
        self._suggestions: list[CompletionSuggestion] = []
        # The prefix in the editor that triggered the dropdown (e.g. "@",
        # "/", "@src/"). Stored so the editor can replace exactly that span
        # when a suggestion is accepted.
        self.trigger_prefix: str = ""

    class SuggestionSelected(Message):
        """Posted when the user accepts a suggestion (Tab or Enter)."""

        def __init__(self, suggestion: CompletionSuggestion, prefix: str) -> None:
            super().__init__()
            self.suggestion = suggestion
            self.prefix = prefix

    class Dismissed(Message):
        """Posted when the user cancels the dropdown (Esc)."""

    def set_suggestions(self, suggestions: list[CompletionSuggestion], *, prefix: str) -> None:
        """Replace the dropdown contents and remember the trigger prefix.

        If ``suggestions`` is empty the dropdown hides itself. The highlighted
        index is set to the best match for the trigger prefix (exact filename
        > filename-startswith > first item), mirroring pi/tui's
        ``getBestAutocompleteMatchIndex`` — typing ``@rea`` should highlight
        ``README.md``, not whatever happened to sort first.
        """

        self._suggestions = list(suggestions)
        self.trigger_prefix = prefix
        self.clear_options()
        label_width = min(28, max((len(suggestion.label) for suggestion in self._suggestions), default=0))
        for sug in self._suggestions:
            prompt = sug.label
            if sug.description:
                prompt = f"{sug.label:<{label_width}}  {sug.description}"
            self.add_option(Option(prompt, id=sug.insert))
        if self._suggestions:
            self.display = True
            self.highlighted = self._best_match_index(prefix)
        else:
            self.display = False

    def _best_match_index(self, prefix: str) -> int:
        """Pick the highlight: exact filename > prefix match > 0.

        ``prefix`` is the editor's trigger token (``"@rea"``, ``"@src/com"``).
        We compare the trailing fragment after the last ``/`` against each
        suggestion's label (with trailing ``/`` stripped for directories).
        """

        if not self._suggestions:
            return 0
        # Pull the trailing fragment out of the trigger token: ``@src/com``
        # → ``com``, ``@rea`` → ``rea``.
        query = prefix.lstrip("@").rsplit("/", 1)[-1].lower()
        if not query:
            return 0
        first_prefix = -1
        for i, sug in enumerate(self._suggestions):
            # Strip a trailing ``/`` so directory labels compare as filenames.
            name = sug.label.rstrip("/").lower()
            if name == query:
                return i
            if first_prefix < 0 and name.startswith(query):
                first_prefix = i
        return first_prefix if first_prefix >= 0 else 0

    def anchor_above(self, target: Widget) -> None:
        """Position the dropdown just above ``target`` so it never overlaps.

        The dropdown is ``dock: top`` so its natural position is the screen's
        top-left corner. We set ``styles.offset`` to move it down to the
        desired row (just above ``target``'s top edge) and right to align with
        ``target``'s left edge. Height is clamped to the available room so a
        short terminal never causes the dropdown to cover the input.
        """

        if not self._suggestions:
            return
        # Borders consume two rows in Textual's box model. Reserve them
        # explicitly so a single suggestion still has one visible content row.
        content_rows = min(len(self._suggestions), 8)
        want = content_rows + 2
        target_region = target.region
        available_above = max(0, target_region.y)
        height = min(want, available_above)
        if height < 3:
            self.hide()
            return
        # Desired top row: as far down as possible without touching the target.
        # Natural y is 0 (dock: top), so offset_y IS the desired top row.
        desired_top = max(0, target_region.y - height)
        offset_x = target_region.x
        self.styles.offset = (offset_x, desired_top)
        self.styles.height = height

    def hide(self) -> None:
        """Hide the dropdown and clear its state."""

        self.display = False
        self.clear_options()
        self._suggestions = []
        self.trigger_prefix = ""

    @property
    def is_open(self) -> bool:
        return self.display and bool(self._suggestions)

    @property
    def suggestions(self) -> list[CompletionSuggestion]:
        """Public read-only view of the current suggestions (for tests/UI)."""

        return list(self._suggestions)

    def move_cursor(self, delta: int) -> None:
        """Move the highlight by ``delta`` (e.g. -1 for up, +1 for down).

        No-op if the dropdown has no suggestions. Wraps around at the ends so
        the user can cycle through the list in either direction.
        """

        if not self._suggestions:
            return
        count = len(self._suggestions)
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = (current + delta) % count

    def action_select(self) -> None:
        """Accept the highlighted suggestion (bound to Tab and Enter).

        We hide the dropdown *synchronously* before posting the message so
        that a fast second Enter can't slip into the editor's submit branch
        before the SuggestionSelected message is processed. The old code left
        ``is_open`` true until a later handler hid the dropdown, creating a
        race where double-Enter would both accept a completion and submit.
        """

        if not self._suggestions or self.highlighted is None:
            return
        sug = self._suggestions[self.highlighted]
        # Hide synchronously: clear display + suggestions so ``is_open``
        # returns False immediately, before the message loop runs again.
        self.display = False
        self._suggestions = []
        self.post_message(self.SuggestionSelected(sug, self.trigger_prefix))

    def action_dismiss(self) -> None:
        """Cancel the dropdown (bound to Esc)."""

        self.post_message(self.Dismissed())


__all__ = ["CompletionDropdown", "CompletionSuggestion"]
