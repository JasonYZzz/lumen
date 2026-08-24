"""Gate that decides whether a slash command may run while an agent run is active.

During a run the app's session/runtime is in flux: the worker is mutating
``self.history``, persisting turns, and holding approval waiters. A command
that swaps the session (``/new``, ``/resume``), rebuilds the runtime
(``/model``), or starts another run (``/retry``, ``/skill:*``) mid-flight can
corrupt that state — turns get appended to the wrong session, a new runtime
replaces the one the worker is using, or two runs fight over the same worker
slot.

This module is the single place that classifies commands by run-safety so the
dispatcher doesn't scatter ``current_worker is running`` checks across a dozen
branches. The classification data lives in the slash-command registry
(``slash_commands.SlashCommand.while_running``); this module only maps it to
policies.

Classification follows the plan's matrix:

* ``allow``  — read-only info (``/mode``, ``/tools``, ``/help``, ``/skills``,
  ``/sessions``, ``/mode <m>``); safe at any time.
* ``block``  — session/runtime or view destroyers (``/new``, ``/resume``, ``/clear``, ``/model``
  switching); refused with a hint to cancel first.
* ``queue``  — run-starters (``/retry``, ``/skill:*``); refused for now (the
  plan allows queuing, but a queue adds re-entrancy complexity — refusing with
  a clear message is the safe minimal behaviour and matches how a new prompt
  is already refused while a run is active).
* ``cancel_then_run`` — ``/exit`` (and its ``/quit`` alias): request
  cancellation, then exit.
"""

from __future__ import annotations

from enum import StrEnum

from lumen.ui.slash_commands import find_command


class CommandPolicy(StrEnum):
    """What the dispatcher should do with a command during an active run."""

    ALLOW = "allow"
    BLOCK = "block"
    QUEUE = "queue"
    CANCEL_THEN_RUN = "cancel_then_run"


# Registry ``while_running`` values map 1:1 onto policies.
_POLICIES: dict[str, CommandPolicy] = {
    "allow": CommandPolicy.ALLOW,
    "block": CommandPolicy.BLOCK,
    "queue": CommandPolicy.QUEUE,
    "cancel_then_run": CommandPolicy.CANCEL_THEN_RUN,
}


def _base_command(line: str) -> str:
    """Return the lower-cased first token of a command line (``/skill:x`` is
    kept intact since its form carries meaning)."""
    stripped = line.strip()
    if not stripped:
        return ""
    first = stripped.split(None, 1)[0]
    return first.lower()


def classify_command(line: str) -> CommandPolicy:
    """Classify ``line`` for run-safety.

    Pure function — no app state read — so it is trivially unit-testable. The
    dispatcher passes the raw command line; the policy tells it how to behave
    when a run is active (and is irrelevant when no run is active).
    """

    command = _base_command(line)
    if not command:
        return CommandPolicy.ALLOW
    if command.startswith("/skill:"):
        # Dynamic prefix form: starting a skill starts a run.
        return CommandPolicy.QUEUE
    entry = find_command(command)
    if entry is None:
        # Unknown command — let it through so the "unknown command" message
        # still fires (no point blocking something that wouldn't do anything
        # anyway).
        return CommandPolicy.ALLOW
    return _POLICIES[entry.while_running]


def classify_model_command(parts: list[str]) -> CommandPolicy:
    """Refine ``/model`` policy based on argument count.

    ``/model`` (list) is read-only; ``/model <name>`` rebuilds the runtime and
    must be blocked during a run.
    """
    if len(parts) >= 2:
        return CommandPolicy.BLOCK
    return CommandPolicy.ALLOW


__all__ = ["CommandPolicy", "classify_command", "classify_model_command"]
