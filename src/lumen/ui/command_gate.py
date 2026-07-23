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
branches.

Classification follows the plan's matrix:

* ``allow``  — read-only info (``/mode``, ``/tools``, ``/help``, ``/skills``,
  ``/sessions``, ``/mode <m>``); safe at any time.
* ``block``  — session/runtime or view destroyers (``/new``, ``/resume``, ``/clear``, ``/model``
  switching); refused with a hint to cancel first.
* ``queue``  — run-starters (``/retry``, ``/skill:*``); refused for now (the
  plan allows queuing, but a queue adds re-entrancy complexity — refusing with
  a clear message is the safe minimal behaviour and matches how a new prompt
  is already refused while a run is active).
* ``cancel_then_run`` — ``/quit``: request cancellation, then exit.
"""

from __future__ import annotations

from enum import StrEnum


class CommandPolicy(StrEnum):
    """What the dispatcher should do with a command during an active run."""

    ALLOW = "allow"
    BLOCK = "block"
    QUEUE = "queue"
    CANCEL_THEN_RUN = "cancel_then_run"


# Read-only info commands: safe at any time, never touch session/runtime.
_ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {
        "/help",
        "/mode",
        "/tools",
        "/skills",
        "/sessions",
    }
)

# Commands that destroy or swap the session/runtime while a worker may be
# mid-run writing turns / using the old runtime. Refused during a run.
_STATE_DESTROYERS: frozenset[str] = frozenset(
    {
        "/new",
        "/resume",
        "/clear",
    }
)


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
    if command in _ALWAYS_ALLOWED:
        return CommandPolicy.ALLOW
    if command in _STATE_DESTROYERS:
        return CommandPolicy.BLOCK
    if command == "/model":
        # ``/model`` with no arg is a read (list); ``/model <name>`` switches
        # the runtime. We can't tell from the command token alone how many
        # parts there are, so the dispatcher must refine — but the safe
        # default for the gate is ALLOW for the listing form and BLOCK for the
        # switching form. The dispatcher checks parts and overrides.
        return CommandPolicy.ALLOW
    if command == "/retry":
        return CommandPolicy.QUEUE
    if command == "/quit":
        return CommandPolicy.CANCEL_THEN_RUN
    if command.startswith("/skill:"):
        return CommandPolicy.QUEUE
    # Unknown command — let it through so the "unknown command" message still
    # fires (no point blocking something that wouldn't do anything anyway).
    return CommandPolicy.ALLOW


def classify_model_command(parts: list[str]) -> CommandPolicy:
    """Refine ``/model`` policy based on argument count.

    ``/model`` (list) is read-only; ``/model <name>`` rebuilds the runtime and
    must be blocked during a run.
    """
    if len(parts) >= 2:
        return CommandPolicy.BLOCK
    return CommandPolicy.ALLOW


__all__ = ["CommandPolicy", "classify_command", "classify_model_command"]
