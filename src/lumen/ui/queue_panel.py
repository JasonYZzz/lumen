"""Compact steering/follow-up queue shown above the composer."""

from __future__ import annotations

from textual.widgets import Static

from lumen.interactive_queue import QueuedMessage, QueueMode


class InteractiveQueuePanel(Static):
    DEFAULT_CSS = """
    InteractiveQueuePanel {
        display: none;
        height: auto;
        max-height: 5;
        margin: 0 2;
        padding: 0 1;
        color: $text-muted;
        background: $surface 65%;
        border-top: solid $primary 35%;
    }
    InteractiveQueuePanel.visible { display: block; }
    """

    def __init__(self) -> None:
        super().__init__("", id="interactive-queue", markup=False)

    def update_messages(self, messages: tuple[QueuedMessage, ...]) -> None:
        if not messages:
            self.update("")
            self.remove_class("visible")
            return
        steering = [item for item in messages if item.mode is QueueMode.STEER]
        follow_ups = [item for item in messages if item.mode is QueueMode.FOLLOW_UP]
        preview = messages[0].text.replace("\n", " ")[:120]
        self.update(
            f"Queued  steer {len(steering)} · follow-up {len(follow_ups)}  │  {preview}\n"
            "Enter steer · Alt+Enter follow-up · Alt+Up edit queued"
        )
        self.add_class("visible")


__all__ = ["InteractiveQueuePanel"]
