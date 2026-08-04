"""Safe compaction: thresholds, anti-thrash and deterministic degradation (M4).

The single safety invariant this module enforces (plan §9.4, §3.2 #5): a
compaction failure must NEVER fall back to the original over-limit history.
Instead it degrades deterministically - reduce tool outputs, then shrink the
recent window - and only if the fixed prefix alone exceeds the window does it
raise :class:`FixedContextTooLarge`, a diagnostic that surfaces before any
provider call. This replaces the legacy ``ContextManager`` path that returned
the original history unchanged on summary failure.

Also provides the soft/hard/target thresholds, the emergency reserve and the
anti-thrash guard (plan §8.3, §9.1): at most ``max_auto_compactions`` automatic
compactions within ``anti_thrash_turns`` user turns; once the limit is hit the
engine stops re-calling the summariser and degrades instead, surfacing
``compaction_thrash`` rather than looping.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart

from lumen.context.artifacts import ArtifactStore
from lumen.context.budget import ContextBudgetExceeded, TokenCounter
from lumen.context.legacy import retain_recent_tokens
from lumen.context.transcript import reduce_tool_outputs


class FixedContextTooLarge(ContextBudgetExceeded):
    """The fixed prefix + current input cannot fit the window (plan §9.4 #5).

    Raised by :func:`degrade_to_window` when no amount of history trimming can
    make the request fit, so the engine surfaces a zone-level diagnostic rather
    than sending an over-limit request.
    """


def _is_summary_prefix(message: ModelMessage) -> bool:
    return (
        isinstance(message, ModelRequest)
        and len(message.parts) == 1
        and isinstance(message.parts[0], SystemPromptPart)
    )


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """Compaction thresholds and anti-thrash limits (plan §8.3, §9.1)."""

    soft_ratio: float = 0.80
    hard_ratio: float = 0.92
    target_ratio: float = 0.55
    emergency_reserve_ratio: float = 0.05
    max_auto_compactions: int = 2
    anti_thrash_turns: int = 5
    failure_cooldown_after: int = 3
    failure_cooldown_turns: int = 3


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Resolved token thresholds for one model window."""

    soft: int
    hard: int
    target: int
    emergency_reserve: int

    @classmethod
    def for_window(cls, window_tokens: int, policy: CompactionPolicy) -> Thresholds:
        return cls(
            soft=int(window_tokens * policy.soft_ratio),
            hard=int(window_tokens * policy.hard_ratio),
            target=int(window_tokens * policy.target_ratio),
            emergency_reserve=max(int(window_tokens * policy.emergency_reserve_ratio), 1),
        )


@dataclass
class CompactionThrashState:
    """Tracks auto-compactions within a sliding turn window (plan §9.1).

    Per session: the count of auto-compactions and the turn index the window
    started at. A manual ``/compact`` does not count toward the auto limit.
    """

    anti_thrash_turns: int = 5
    auto_count: int = 0
    window_start_turn: int = 0
    turn: int = 0
    consecutive_failures: int = 0
    cooldown_until_turn: int = 0
    total_successes: int = 0
    total_failures: int = 0

    def advance_turn(self) -> None:
        """Record one user turn; expire the thrash window when it elapses."""

        self.turn += 1
        if self.turn - self.window_start_turn >= self.anti_thrash_turns:
            self.auto_count = 0
            self.window_start_turn = self.turn

    def record_auto_compaction(self) -> None:
        self.auto_count += 1

    def record_success(self) -> None:
        self.total_successes += 1
        self.consecutive_failures = 0
        self.cooldown_until_turn = 0

    def record_failure(self, policy: CompactionPolicy) -> None:
        self.total_failures += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= policy.failure_cooldown_after:
            self.cooldown_until_turn = self.turn + policy.failure_cooldown_turns

    def cooling_down(self) -> bool:
        return self.turn < self.cooldown_until_turn

    def thrashed(self, policy: CompactionPolicy) -> bool:
        """Whether the auto-compaction limit for the window is reached."""

        return self.auto_count >= policy.max_auto_compactions


def degrade_to_window(
    history: Sequence[ModelMessage],
    *,
    window_tokens: int,
    fixed_tokens: int,
    output_reserve: int,
    counter: TokenCounter,
    store: ArtifactStore | None,
    keep_recent_tokens: int,
) -> list[ModelMessage]:
    """Deterministically shrink ``history`` to fit the window; never over-limit.

    Degradation order (plan §9.4): (1) receipt-ize tool outputs, (2) shrink the
    recent window progressively to the safe boundary, (3) keep only the last
    coherent user turn. If the fixed prefix + output reserve already exceeds
    the window, raise :class:`FixedContextTooLarge` instead of returning an
    over-limit history.
    """

    budget = window_tokens - output_reserve - fixed_tokens
    if budget <= 0:
        raise FixedContextTooLarge(
            "fixed context footprint cannot fit the window even with no "
            f"history: fixed={fixed_tokens} + reserve={output_reserve} > "
            f"window={window_tokens}"
        )

    messages = list(history)
    # A successful compaction's leading system-only request is the durable
    # representation of the already-covered delta. It must survive emergency
    # history reduction or the checkpoint would advance while its summary
    # silently disappears from the provider context.
    summary_prefix: list[ModelMessage] = []
    while messages and _is_summary_prefix(messages[0]):
        summary_prefix.append(messages.pop(0))
    prefix_tokens = counter.count_messages(summary_prefix).tokens
    if prefix_tokens > budget:
        raise FixedContextTooLarge(
            "compaction summary cannot fit after fixed context and output "
            f"reserve (summary={prefix_tokens}, history_budget={budget})"
        )
    if _fits([*summary_prefix, *messages], budget, counter):
        return [*summary_prefix, *messages]

    # Step 1: reduce tool outputs to receipts (no re-summarisation).
    if store is not None:
        # Emergency mode may receipt-ize even the newest oversized body. The
        # receipt keeps tool name/call id/status plus diagnostic head/tail while
        # the complete body remains addressable in the artifact store.
        reduced = reduce_tool_outputs(messages, store, keep_recent_full=0)
        messages = reduced.messages
        if _fits([*summary_prefix, *messages], budget, counter):
            return [*summary_prefix, *messages]

    # Step 2: shrink the recent window progressively, snapped to a safe boundary.
    for keep in _shrinking_budgets(keep_recent_tokens):
        recent = retain_recent_tokens(messages, keep)
        if recent and _fits([*summary_prefix, *recent], budget, counter):
            return [*summary_prefix, *recent]

    # Step 3: keep only the last coherent user turn (smallest safe window).
    last_boundary = _last_safe_boundary(messages)
    if last_boundary and _fits([*summary_prefix, *last_boundary], budget, counter):
        return [*summary_prefix, *last_boundary]
    if summary_prefix:
        return summary_prefix

    # Nothing fits: the fixed prefix dominates. Fail loudly, never over-limit.
    raise FixedContextTooLarge(
        "history cannot be reduced to fit the window after deterministic "
        f"degradation (budget={budget} tokens after fixed={fixed_tokens} "
        f"+ reserve={output_reserve}); reduce the prompt/instructions or "
        "raise the model window"
    )


def _fits(messages: Sequence[ModelMessage], budget: int, counter: TokenCounter) -> bool:
    return counter.count_messages(messages).tokens <= budget


def _shrinking_budgets(keep_recent_tokens: int) -> list[int]:
    """A progression of shrinking recent-window budgets to try in degradation."""

    steps = [keep_recent_tokens, keep_recent_tokens // 2, keep_recent_tokens // 4]
    return [max(s, 1) for s in steps if s > 0]


def _last_safe_boundary(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """The trailing window from the last user-request boundary (plan §9.2 #6)."""

    retained = retain_recent_tokens(messages, 1)
    return list(retained)


__all__ = [
    "CompactionPolicy",
    "CompactionThrashState",
    "FixedContextTooLarge",
    "Thresholds",
    "degrade_to_window",
]
