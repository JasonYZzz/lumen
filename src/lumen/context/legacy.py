"""Provider-independent context estimation and compaction.

The agent's active model context is rebuilt from a structured summary plus a
token-budgeted recent window when the estimated token usage crosses the soft
limit. The session repository still preserves the full append-only JSONL
history, so compaction only changes what the model sees, not what is recorded.

The compaction design mirrors coding-agent's approach
(``pi/packages/coding-agent/src/core/compaction/``):

1. **Token-budgeted cut point** — a backward walk accumulates tokens until
   ``keep_recent_tokens`` is spent, then snaps forward to a safe boundary
   (start of a user-prompt request) so a tool result is never orphaned from
   its call. Replaces the older fixed-turn-count cut, which was unpredictable.

2. **Per-tool-result truncation at summary time** — each tool result is
   capped to ``summary_tool_result_chars`` before joining, so one giant
   output can't crowd out the rest. coding-agent's ``TOOL_RESULT_MAX_CHARS``.

3. **Iterative summary update** — when a prior summary exists, it's passed to
   the summarizer as ``<previous-summary>`` so the model preserves existing
   entries and only adds new ones, rather than re-interpreting the whole
   history from scratch each compaction.
"""

from __future__ import annotations

import json
import math
import warnings
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from lumen.config import ContextConfig
from lumen.events import (
    ContextCompactionCompleted,
    ContextCompactionFailed,
    ContextCompactionStarted,
    RunEvent,
)
from lumen.plan import PlanState

EventSink = Callable[[RunEvent], Awaitable[None]]


class ContextSummary(BaseModel):
    """Structured summary the compaction model must produce.

    These eight fields are the public surface area that the next run needs to
    continue work: what the goal is, what's done, what's pending, what failed
    and what the model should remember. The schema is intentionally narrow so
    a misbehaving model cannot dump arbitrary content into the active context.
    """

    model_config = ConfigDict(extra="forbid")

    goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    completed: list[str] = Field(default_factory=list)
    current_plan: list[str] = Field(default_factory=list)
    important_files: list[str] = Field(default_factory=list)
    key_facts: list[str] = Field(default_factory=list)
    failures_and_approvals: list[str] = Field(default_factory=list)
    outstanding: list[str] = Field(default_factory=list)


class ContextBudgetExceeded(ValueError):
    """Raised before a provider request whose fixed footprint cannot fit."""


@dataclass(frozen=True, slots=True)
class ContextReservation:
    """Token footprint added to history for the next provider request."""

    prompt_tokens: int = 0
    instructions_tokens: int = 0
    tool_schema_tokens: int = 0
    safety_tokens: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.prompt_tokens,
                self.instructions_tokens,
                self.tool_schema_tokens,
                self.safety_tokens,
            )
            < 0
        ):
            raise ValueError("context reservation values must be non-negative")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.instructions_tokens + self.tool_schema_tokens + self.safety_tokens


class RequestBudgetEstimator:
    """Estimate the complete fixed and variable footprint of one request."""

    def estimate_tool_schemas(self, schemas: Sequence[Any]) -> int:
        rendered = json.dumps(list(schemas), ensure_ascii=False, sort_keys=True, default=str)
        return math.ceil(len(rendered.encode("utf-8")) / 4)

    def build_reservation(
        self,
        *,
        prompt: str,
        instructions: str,
        tool_schemas: Sequence[Any],
        safety_tokens: int,
    ) -> ContextReservation:
        return ContextReservation(
            prompt_tokens=estimate_message_tokens([ModelRequest(parts=[UserPromptPart(content=prompt)])]),
            instructions_tokens=estimate_message_tokens(
                [ModelRequest(parts=[SystemPromptPart(content=instructions)])]
            ),
            tool_schema_tokens=self.estimate_tool_schemas(tool_schemas),
            safety_tokens=max(0, safety_tokens),
        )


@dataclass(frozen=True, slots=True)
class CompactionRecord:
    summary: ContextSummary
    active_history: list[ModelMessage]
    source_message_count: int
    usage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreparedContext:
    history: list[ModelMessage]
    compaction: CompactionRecord | None
    usage: dict[str, Any] = field(default_factory=dict[str, Any])


def _part_text(part: Any) -> str:
    """Best-effort textual rendering of one message part for token counting."""

    content = getattr(part, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(part, ToolReturnPart):
        rendered = part.content
        if not isinstance(rendered, str):
            try:
                rendered = json.dumps(rendered, ensure_ascii=False)
            except (TypeError, ValueError):
                rendered = str(rendered)
        return str(rendered)
    if isinstance(part, ToolCallPart):
        args = part.args
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                args = str(args)
        return str(args)
    return ""


def estimate_message_tokens(messages: Sequence[ModelMessage]) -> int:
    """Provider-independent token estimate.

    We round the serialised UTF-8 byte length of every part's textual content
    up to a quarter (roughly four bytes per token). This deliberately overcounts
    structured parts rather than underestimate them; the soft limit is a hint,
    not a hard budget.
    """

    if not messages:
        return 0
    total_bytes = 0
    for message in messages:
        for part in getattr(message, "parts", []):
            total_bytes += len(_part_text(part).encode("utf-8"))
    return math.ceil(total_bytes / 4)


def _is_safe_cut_boundary(message: ModelMessage) -> bool:
    """Whether ``message`` is a safe place to start the retained window.

    A safe boundary is the start of a user-prompt-initiated request — cutting
    there guarantees the retained window begins coherently and never orphans a
    tool result from its preceding tool call (Anthropic/OpenAI APIs reject an
    orphaned tool_result). Mirrors coding-agent's ``isCutPointMessage``.
    """

    if not isinstance(message, ModelRequest):
        return False
    parts = message.parts
    non_system = [p for p in parts if not isinstance(p, SystemPromptPart)]
    return bool(non_system) and isinstance(non_system[0], UserPromptPart)


def _is_summary_prefix(message: ModelMessage) -> bool:
    """Whether ``message`` is the compaction summary prefix.

    The prefix is a ``ModelRequest`` whose only part is a ``SystemPromptPart``
    (the formatted ``ContextSummary``). It carries no user request and no tool
    association, so the invariants must skip it when validating.
    """
    if not isinstance(message, ModelRequest):
        return False
    parts = message.parts
    return len(parts) == 1 and isinstance(parts[0], SystemPromptPart)


def validate_active_history(messages: Sequence[ModelMessage]) -> list[str]:
    """Check ``messages`` against the compaction safety invariants.

    Returns a list of human-readable error strings (empty when valid). The
    invariants enforced:

    * The first non-summary message is a user request (so the provider sees a
      coherent conversation start).
    * No message starts with a ``ToolReturnPart`` — a return with no preceding
      call would be rejected by Anthropic/OpenAI as an orphaned tool result.
    * Every ``ToolReturnPart`` has a matching preceding ``ToolCallPart`` by
      ``tool_call_id``.

    The summary prefix (a SystemPromptPart-only request from ``_build_active``)
    is skipped — it is metadata, not a conversational turn and not part of any
    tool association.
    """

    errors: list[str] = []
    # Skip a leading summary prefix; it's metadata, not a conversational turn.
    effective = list(messages)
    while effective and _is_summary_prefix(effective[0]):
        effective.pop(0)
    if not effective:
        return errors

    # Invariant: the first real conversational message is a user request.
    first = effective[0]
    if not isinstance(first, ModelRequest):
        errors.append("active history does not start with a user request")
    else:
        parts = first.parts
        non_system = [p for p in parts if not isinstance(p, SystemPromptPart)]
        if not non_system or not isinstance(non_system[0], UserPromptPart):
            if any(isinstance(p, ToolReturnPart) for p in non_system):
                errors.append(
                    "active history does not start with a user request: "
                    "it begins with a ToolReturnPart (orphaned tool result)"
                )
            else:
                errors.append("active history does not start with a user request")

    # Invariant: every ToolReturnPart has a preceding matching ToolCallPart.
    seen_calls: set[str] = set()
    for message in effective:
        parts = getattr(message, "parts", [])
        for part in parts:
            if isinstance(part, ToolCallPart):
                call_id = getattr(part, "tool_call_id", None)
                if call_id:
                    seen_calls.add(call_id)
            elif isinstance(part, ToolReturnPart):
                call_id = getattr(part, "tool_call_id", None)
                if call_id is None or call_id not in seen_calls:
                    label = call_id or "<no id>"
                    errors.append(f"ToolReturnPart {label!r} has no preceding matching ToolCallPart")
    return errors


def retain_recent_tokens(
    messages: Sequence[ModelMessage],
    keep_tokens: int,
) -> list[ModelMessage]:
    """Return the trailing window of ``messages`` fitting in ``keep_tokens``.

    Walks backwards accumulating token estimates until the budget is spent,
    then snaps forward to the next safe boundary (start of a user-prompt
    request). This is coding-agent's ``findCutPoint`` algorithm: predictable
    post-compaction size regardless of turn granularity, and a tool result is
    never separated from its call.

    If the very first message already exceeds the budget we keep it anyway —
    dropping the user's original request would be worse than a slightly
    oversized window.
    """

    if keep_tokens <= 0 or not messages:
        return []
    accumulated = 0
    cut_index = 0
    for index in range(len(messages) - 1, -1, -1):
        accumulated += estimate_message_tokens([messages[index]])
        if accumulated >= keep_tokens:
            # Prefer the next user boundary. When the budget is crossed inside
            # the newest turn there is no later boundary; in that case move
            # backward to the start of that turn rather than returning an
            # assistant-only suffix.
            forward = [i for i in range(index, len(messages)) if _is_safe_cut_boundary(messages[i])]
            if forward:
                cut_index = forward[0]
            else:
                backward = [i for i in range(index - 1, -1, -1) if _is_safe_cut_boundary(messages[i])]
                if not backward:
                    return []
                cut_index = backward[0]
            break
    retained = list(messages[cut_index:])
    if validate_active_history(retained):
        # Defensive fallback for malformed provider history: retain the newest
        # coherent user turn, or nothing when no safe boundary exists.
        boundaries = [i for i, message in enumerate(messages) if _is_safe_cut_boundary(message)]
        return list(messages[boundaries[-1] :]) if boundaries else []
    return retained


def retain_recent_turns(messages: Sequence[ModelMessage], keep_turns: int) -> list[ModelMessage]:
    """Backwards-compatible turn-count cut. Prefer :func:`retain_recent_tokens`.

    Kept so older tests and any external callers still compile; the compaction
    pipeline now uses the token-budgeted cut. A "turn" starts at a
    ``ModelRequest`` whose first non-system part is a user prompt.
    """

    if keep_turns <= 0 or not messages:
        return []
    boundaries: list[int] = []
    for index, message in enumerate(messages):
        if isinstance(message, ModelRequest):
            parts = message.parts
            non_system = [p for p in parts if not isinstance(p, SystemPromptPart)]
            if non_system and isinstance(non_system[0], UserPromptPart):
                boundaries.append(index)
    if not boundaries:
        return list(messages[-keep_turns * 2 :]) if keep_turns else []
    start_index = boundaries[max(0, len(boundaries) - keep_turns)]
    return list(messages[start_index:])


@dataclass
class ContextManager:
    """Compacts the active model context when it crosses the soft limit.

    .. deprecated:: M1
        Retained as the implementation behind :class:`lumen.context.ContextEngine`.
        New callers should depend on ``ContextEngine.prepare``/``commit``/``control``
        rather than constructing a ``ContextManager`` directly; this type is
        removed at the end of the deprecation period (M8).

    Compaction uses a dedicated, tool-free agent with a strict structured output
    type so the model cannot free-form ramble into the active context. On any
    failure the original history is returned unchanged.
    """

    config: ContextConfig
    model: Model | str
    #: Internal-use flag: the :class:`ContextEngine` constructs the manager it
    #: wraps with ``_internal=True`` so its own construction does not emit the
    #: deprecation warning intended for external callers.
    _internal: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self._internal:
            warnings.warn(
                "lumen.context.ContextManager is deprecated; use "
                "lumen.context.ContextEngine instead. The manager is retained as "
                "an internal implementation and will be removed in M8.",
                DeprecationWarning,
                stacklevel=2,
            )

    async def prepare(
        self,
        history: Sequence[ModelMessage],
        plan: PlanState,
        diagnostics: Sequence[dict[str, Any]],
        emit: EventSink,
        *,
        previous_summary: ContextSummary | None = None,
        reservation: ContextReservation | None = None,
        current_prompt: str | None = None,
        instructions_estimate: int = 0,
        tool_schema_estimate: int = 0,
    ) -> PreparedContext:
        """Return the history the next run should use, compacting if needed.

        ``previous_summary`` lets an earlier compaction's summary be carried
        forward for iterative update — the summarizer merges new history into
        the prior summary rather than rebuilding from scratch, preventing drift
        on long sessions that compact multiple times.

        ``current_prompt`` / ``instructions_estimate`` / ``tool_schema_estimate``
        reserve space for the upcoming request's own footprint: the recent
        window is sized so that window + prompt + system instructions + tool
        schema stays within ``keep_recent_tokens``, preventing a compacted
        window that — combined with a large new prompt — still blows the
        provider context limit.
        """

        if reservation is None:
            prompt_tokens = (
                estimate_message_tokens([ModelRequest(parts=[UserPromptPart(content=current_prompt)])])
                if current_prompt
                else 0
            )
            reservation = ContextReservation(
                prompt_tokens=prompt_tokens,
                instructions_tokens=max(0, instructions_estimate),
                tool_schema_tokens=max(0, tool_schema_estimate),
            )
        elif current_prompt is not None or instructions_estimate or tool_schema_estimate:
            raise ValueError("pass reservation or legacy reservation arguments, not both")

        if reservation.total_tokens > self.config.soft_token_limit:
            raise ContextBudgetExceeded(
                "request footprint exceeds context.soft_token_limit before history is added: "
                f"{reservation.total_tokens} > {self.config.soft_token_limit} tokens"
            )

        if not self.config.enabled:
            return PreparedContext(history=list(history), compaction=None)

        estimate = estimate_message_tokens(history)
        if estimate + reservation.total_tokens < self.config.soft_token_limit:
            return PreparedContext(history=list(history), compaction=None)

        await emit(ContextCompactionStarted(source_message_count=len(history)))
        try:
            summary, summary_usage = await self._summarize(history, plan, diagnostics, previous_summary)
        except Exception as error:
            await emit(ContextCompactionFailed(message=str(error)))
            return PreparedContext(history=list(history), compaction=None)

        active_history = self._build_active(
            history,
            summary,
            reservation=reservation,
        )
        invariant_errors = validate_active_history(active_history)
        if invariant_errors:
            message = "; ".join(invariant_errors)
            await emit(ContextCompactionFailed(message=message))
            return PreparedContext(history=list(history), compaction=None)
        record = CompactionRecord(
            summary=summary,
            active_history=active_history,
            source_message_count=len(history),
            usage=summary_usage,
        )
        await emit(
            ContextCompactionCompleted(
                active_message_count=len(active_history),
                summary_tokens_estimate=estimate_message_tokens(active_history),
            )
        )
        return PreparedContext(history=active_history, compaction=record, usage=summary_usage)

    async def _summarize(
        self,
        history: Sequence[ModelMessage],
        plan: PlanState,
        diagnostics: Sequence[dict[str, Any]],
        previous_summary: ContextSummary | None,
    ) -> tuple[ContextSummary, dict[str, Any]]:
        instructions = self._summary_instructions(plan, diagnostics, previous_summary)
        agent: Agent[None, ContextSummary] = Agent(
            self.model,
            output_type=ContextSummary,
            deps_type=type(None),
            system_prompt=instructions,
            model_settings=ModelSettings(max_tokens=self.config.summary_max_tokens),
            retries=0,
        )
        serialised = self._serialize_for_summary(history)
        result = await agent.run(serialised, usage_limits=UsageLimits(request_limit=1))
        usage: RunUsage = result.usage
        return result.output, asdict(usage)

    def _summary_instructions(
        self,
        plan: PlanState,
        diagnostics: Sequence[dict[str, Any]],
        previous_summary: ContextSummary | None,
    ) -> str:
        plan_lines = (
            "\n".join(f"- {step.id}: {step.title} [{step.status.value}]" for step in plan.steps) or "(empty)"
        )
        diagnostic_lines = "\n".join(json.dumps(item) for item in diagnostics) or "(none)"
        base = (
            "Summarize the prior conversation into the structured schema. "
            "Capture only what is needed to continue: goals, constraints, what is "
            "already completed, the current plan, important files, key facts, "
            "approval and failure history, and outstanding work. Do not include "
            "private chain-of-thought or detailed reasoning; keep entries short.\n\n"
            f"Current plan:\n{plan_lines}\n\nDiagnostics:\n{diagnostic_lines}\n"
        )
        if previous_summary is not None:
            # Iterative update: pass the prior summary so the model PRESERVES
            # existing entries and only adds/updates with new information.
            # Mirrors coding-agent's UPDATE_SUMMARIZATION_PROMPT. Without this,
            # every compaction re-interprets the whole tail from scratch and
            # early decisions the summarizer happens to drop stay dropped.
            prior_json = previous_summary.model_dump_json(indent=2)
            return (
                base + "\n<previous-summary>\n" + prior_json + "\n</previous-summary>\n\n"
                "This is an UPDATE. PRESERVE all existing entries from the previous "
                "summary unless contradicted by the new conversation. ADD new progress, "
                "decisions, and context. Move completed items from outstanding/current "
                "into completed."
            )
        return base

    def _serialize_for_summary(self, history: Sequence[ModelMessage]) -> str:
        """Render history as text for the summarizer, with per-result truncation.

        Each ``ToolReturnPart`` is capped to ``summary_tool_result_chars`` so
        one giant output can't crowd out the rest — coding-agent's pattern.
        We no longer apply a global prefix slice, which silently dropped the
        most recent (most relevant) turns in long sessions.
        """

        cap = self.config.summary_tool_result_chars
        rendered: list[str] = []
        for message in history:
            role = "assistant" if isinstance(message, ModelResponse) else "user"
            for part in getattr(message, "parts", []):
                # ToolReturnPart and ToolCallPart must be checked BEFORE the
                # generic ``content`` string check, because they also expose a
                # ``content``/``args`` attribute that would match the string
                # branch and bypass per-result truncation.
                if isinstance(part, ToolReturnPart):
                    text = str(part.content)
                    if len(text) > cap:
                        text = text[:cap] + f"\n[... {len(text) - cap} more chars truncated]"
                    rendered.append(f"tool result ({part.tool_name}): {text}")
                elif isinstance(part, ToolCallPart):
                    rendered.append(f"tool call ({part.tool_name}): {part.args}")
                else:
                    content = getattr(part, "content", None)
                    if isinstance(content, str) and content:
                        rendered.append(f"{role}: {content}")
        return "\n".join(rendered)

    def _build_active(
        self,
        history: Sequence[ModelMessage],
        summary: ContextSummary,
        *,
        reservation: ContextReservation | None = None,
        current_prompt: str | None = None,
        instructions_estimate: int = 0,
        tool_schema_estimate: int = 0,
    ) -> list[ModelMessage]:
        summary_text = self._format_summary(summary)
        prefix = ModelRequest(parts=[SystemPromptPart(content=summary_text)])
        # Reserve tokens for the upcoming request's own footprint so the
        # compacted window + new prompt + instructions + tool schema respects
        # the budget. Without this, a large new prompt could push the total
        # past the provider's context window even after compaction.
        if reservation is None:
            prompt_estimate = (
                estimate_message_tokens([ModelRequest(parts=[UserPromptPart(content=current_prompt)])])
                if current_prompt
                else 0
            )
            reservation = ContextReservation(
                prompt_tokens=prompt_estimate,
                instructions_tokens=max(0, instructions_estimate),
                tool_schema_tokens=max(0, tool_schema_estimate),
            )
        summary_tokens = estimate_message_tokens([prefix])
        available = self.config.soft_token_limit - reservation.total_tokens - summary_tokens
        keep_tokens = max(0, min(self.config.keep_recent_tokens, available))
        # Token-budgeted cut: keeps a predictable-size recent window regardless
        # of turn granularity, and never orphans a tool result from its call.
        recent = retain_recent_tokens(history, keep_tokens)
        return [prefix, *recent]

    @staticmethod
    def _format_summary(summary: ContextSummary) -> str:
        lines = ["Prior conversation summary:"]
        for label, items in (
            ("Goals", summary.goals),
            ("Constraints", summary.constraints),
            ("Completed", summary.completed),
            ("Current plan", summary.current_plan),
            ("Important files", summary.important_files),
            ("Key facts", summary.key_facts),
            ("Failures and approvals", summary.failures_and_approvals),
            ("Outstanding", summary.outstanding),
        ):
            if items:
                lines.append(f"{label}:")
                lines.extend(f"  - {item}" for item in items)
        return "\n".join(lines)


__all__ = [
    "CompactionRecord",
    "ContextBudgetExceeded",
    "ContextConfig",
    "ContextManager",
    "ContextReservation",
    "ContextSummary",
    "PreparedContext",
    "RequestBudgetEstimator",
    "estimate_message_tokens",
    "retain_recent_tokens",
    "retain_recent_turns",
    "validate_active_history",
]
