from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, cast

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    DeferredToolRequests,
    DeferredToolResults,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    Tool,
    ToolApproved,
    ToolDenied,
    UsageLimits,
)
from pydantic_ai.capabilities import HandleDeferredToolCalls
from pydantic_ai.exceptions import IncompleteToolCall, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import DeferredToolApprovalResult
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from lumen.config import LimitsConfig
from lumen.context import (
    ContextManager,
    ContextSummary,
    PreparedContext,
    RequestBudgetEstimator,
    estimate_message_tokens,
)
from lumen.events import (
    ApprovalRequest,
    CommentaryDelta,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    TextDelta,
    TextRetracted,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    ToolExecutionDiagnostic,
    UsageUpdated,
)
from lumen.interactive_queue import InteractiveInputCapability, InteractiveMessageQueue
from lumen.plan import PlanState
from lumen.task_control import TaskController
from lumen.tools.execution import RecoverableToolErrors

EventSink = Callable[[RunEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ToolApproval:
    approved: bool
    message: str = "The user denied this tool call."


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ToolApproval]]


def _merge_usage(*items: dict[str, Any]) -> dict[str, Any]:
    """Add numeric usage fields and nested detail counters."""

    merged: dict[str, Any] = {}
    details: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            if key == "details" and isinstance(value, dict):
                for detail, count in cast(dict[str, Any], value).items():
                    if isinstance(count, int):
                        details[detail] = details.get(detail, 0) + count
            elif isinstance(value, int):
                merged[key] = int(merged.get(key, 0)) + value
            elif key not in merged:
                merged[key] = value
    if details:
        merged["details"] = details
    return merged


def _approval_message_field(message: str, field_name: str) -> str | None:
    marker = f"{field_name}="
    if marker not in message:
        return None
    return message.split(marker, 1)[1].split(",", 1)[0].rstrip(".)")


def _tool_result_status(raw: Any, *, outcome: str) -> tuple[int | None, str, str | None]:
    exit_code: int | None = None
    if isinstance(raw, dict):
        result = cast(dict[str, Any], raw)
        value = result.get("exit_code")
        if isinstance(value, int) and not isinstance(value, bool):
            exit_code = value
        if result.get("cancelled") is True:
            return exit_code, "cancelled", "cancelled"
        if result.get("timed_out") is True:
            return exit_code, "timeout", "timeout"
        if exit_code not in {None, 0}:
            return exit_code, "tool_error", "nonzero_exit"
    if outcome != "success":
        return exit_code, "tool_error", "tool_error"
    return exit_code, "success", None


def _append_unfinished_diagnostics(
    diagnostics: list[dict[str, Any]],
    *,
    start_times: dict[str, float],
    tool_names: dict[str, str],
    finished_calls: set[str],
    status: str,
    category: str,
) -> None:
    now = time.monotonic()
    for call_id, started in start_times.items():
        if call_id in finished_calls:
            continue
        diagnostics.append(
            ToolExecutionDiagnostic(
                call_id=call_id,
                name=tool_names.get(call_id, "<unknown>"),
                status=status,
                error_category=category,
                elapsed_seconds=max(0.0, now - started),
                message=f"tool {status}",
            ).to_dict()
        )


def _tool_schema_document(tool: Tool[Any]) -> dict[str, Any]:
    schema = tool.function_schema
    return {
        "name": tool.name,
        "description": tool.description or schema.description or "",
        "parameters": schema.json_schema,
        "returns": schema.return_schema,
    }


CONTROL_INSTRUCTIONS = """
For work requiring three or more actions, any file mutation, command execution,
or several coordinated tools, call set_plan before acting. Use report_progress
only for concise public updates: findings, changes, errors, recovery, and next
action. Never place private reasoning or hidden chain-of-thought in progress.
Keep plan steps current and complete or block each step before the final answer.
"""

#: Maximum retry attempts for transient provider errors (429, 5xx, connection
#: resets). Each retry waits ``_RETRY_DELAYS[i]`` seconds (exponential backoff).
_RETRY_MAX_ATTEMPTS = 3
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


def _is_transient(error: BaseException) -> bool:
    """Whether ``error`` is a transient provider/network failure worth retrying.

    We retry on connection errors, timeouts, and generic API errors that often
    wrap 429/503. We do NOT retry on usage limits, tool-call truncation,
    cancellations, or validation errors — those need user action.
    """

    # Connection / network layer
    if isinstance(error, ConnectionError | TimeoutError | asyncio.TimeoutError):
        return True
    # Pydantic AI model HTTP errors (carry status_code when available)
    for attr in ("status_code", "status"):
        code = getattr(error, attr, None)
        if isinstance(code, int) and code in (408, 429, 500, 502, 503, 504):
            return True
    # Message-based heuristic for wrapped HTTP errors without status_code attr
    msg = str(error).lower()
    if any(kw in msg for kw in ("rate limit", "overloaded", "service unavailable", "temporarily")):
        return True
    return False


def _control_tool(name: str, controller: TaskController) -> Tool[None]:
    """Wrap a bound control method as a sequential, model-visible tool.

    Control tools are side-effect-free with respect to the workspace: they only
    mutate plan state inside the controller. They carry metadata that the
    runtime surfaces in tool events so the TUI can render them distinctly.
    """

    method_map: dict[str, Callable[..., Awaitable[str]]] = {
        "set_plan": controller.set_plan,
        "update_step": controller.update_step,
        "report_progress": controller.report_progress,
    }
    method = method_map[name]

    return Tool(
        method,
        name=name,
        sequential=True,
        requires_approval=False,
        metadata={"origin": "control", "risk": "read", "control": "true"},
    )


def _friendly_truncation_message(error: IncompleteToolCall) -> str:
    """User-facing message for tool-call truncation.

    pydantic AI raises ``IncompleteToolCall`` when a model response is cut off
    mid-tool-call (``finish_reason='length'``). The raw message is technical
    ("Model token limit exceeded while generating a tool call..."); we surface
    a clearer hint that points at the ``max_tokens`` setting rather than
    suggesting the prompt itself was wrong.
    """

    return (
        "Output was truncated mid-tool-call before any tool could run. "
        "Increase `max_tokens` in the model's `settings:` block, or split the "
        "task into smaller steps. Original: " + str(error)
    )


def _friendly_limit_message(error: UsageLimitExceeded, limits: LimitsConfig) -> str:
    """User-facing message when a usage limit halts the run.

    pydantic AI raises ``UsageLimitExceeded`` when ``request_count`` or
    ``tool_calls`` trips. There is no ``total_tokens`` cap (removed — context
    is managed by auto-compaction instead), so the message only names the two
    anti-runaway guards that remain.
    """

    return (
        f"Run stopped at a usage limit. Configured caps are "
        f"request_count={limits.request_count}, tool_calls={limits.tool_calls}. "
        f"Raise the relevant value under `agent.limits:` in agent.yaml to let "
        f"the run continue. Detail: {error}"
    )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    output: str
    new_messages: list[ModelMessage]
    usage: dict[str, Any]
    approvals: list[dict[str, Any]]
    plan: PlanState = field(default_factory=PlanState)
    active_history: list[ModelMessage] = field(default_factory=list[ModelMessage])
    diagnostics: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    compaction: Any = None


@dataclass(frozen=True, slots=True)
class PartialRunOutcome:
    """Audit record for a run that failed or was cancelled.

    A successful run returns a full :class:`RunOutcome`; a failed/cancelled run
    raises, but the work done before the failure (approvals already decided,
    the plan snapshot, usage so far, diagnostics, any streamed text) is still
    valuable for auditing and for persisting a faithful session turn rather
    than an empty one. The runtime attaches a ``PartialRunOutcome`` to the
    raised exception (``exc.partial_outcome``) so the caller can recover it
    without changing the control-flow contract.
    """

    status: str
    message: str
    approvals: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    usage: dict[str, Any] = field(default_factory=dict[str, Any])
    plan: PlanState = field(default_factory=PlanState)
    diagnostics: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    partial_text: str = ""
    retryable: bool = False


def attach_partial_outcome(error: BaseException, partial: PartialRunOutcome) -> BaseException:
    """Set ``partial_outcome`` on ``error`` and return it.

    ``CancelledError`` and generic ``Exception`` are not our classes, so we
    attach the audit record as an attribute rather than subclass. Returns the
    same exception so callers can ``raise attach_partial_outcome(exc, ...)``.
    """
    try:
        object.__setattr__(error, "partial_outcome", partial)
    except (AttributeError, TypeError):
        # Some built-in exceptions reject arbitrary attributes; fall back to a
        # dict slot if possible, otherwise the caller must read the event sink.
        pass
    return error


def get_partial_outcome(error: BaseException) -> PartialRunOutcome | None:
    """Read a ``PartialRunOutcome`` attached by :func:`attach_partial_outcome`."""
    return getattr(error, "partial_outcome", None)


class AgentRuntime:
    def __init__(
        self,
        *,
        model: Model | str,
        tools: Sequence[Tool[None]],
        toolsets: Sequence[AbstractToolset[None]],
        instructions: str,
        limits: LimitsConfig,
        tool_metadata: dict[str, dict[str, str]],
        model_settings: ModelSettings | None = None,
        context_manager: ContextManager | None = None,
        tool_schema_documents: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.limits = limits
        self.tool_metadata = tool_metadata
        # Store the instructions text so the context manager can reserve space
        # for them in the compaction budget (they're sent with every request).
        self.instructions = instructions
        self.controller = TaskController()
        self.context_manager = context_manager
        self.interactive_queue = InteractiveMessageQueue()
        control_tools = [
            _control_tool(name, self.controller) for name in ("set_plan", "update_step", "report_progress")
        ]
        self.request_budget_estimator = RequestBudgetEstimator()
        self.tool_schema_documents = [
            *(_tool_schema_document(tool) for tool in [*control_tools, *tools]),
            *tool_schema_documents,
        ]
        self.agent: Agent[None, str] = Agent(
            model,
            instructions=instructions,
            deps_type=type(None),
            tools=[*control_tools, *tools],
            toolsets=toolsets,
            model_settings=model_settings,
            tool_timeout=limits.tool_timeout_seconds,
        )

    def _estimate_tool_schema_tokens(self) -> int:
        return self.request_budget_estimator.estimate_tool_schemas(self.tool_schema_documents)

    async def run(
        self,
        prompt: str,
        history: Sequence[ModelMessage],
        emit: EventSink,
        approve: ApprovalHandler,
        *,
        plan: PlanState | None = None,
        previous_summary: ContextSummary | None = None,
    ) -> RunOutcome:
        await emit(RunStarted(prompt))
        self.controller.start(plan or PlanState(), emit)

        approval_log: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        started_at = time.monotonic()
        start_times: dict[str, float] = {}
        tool_names: dict[str, str] = {}
        finished_calls: set[str] = set()
        tool_call_count = 0
        run_usage = RunUsage()
        context_estimate = estimate_message_tokens(history)

        # Compaction rebuilds the active model context from a structured summary
        # plus the recent complete turns. The session repository still preserves
        # the full raw history; only what the model sees changes.
        # ``previous_summary`` (from the last compaction in this session) lets
        # the summarizer do an iterative update instead of rebuilding from
        # scratch — preventing summary drift on long sessions.
        prepared: PreparedContext | None = None
        if self.context_manager is not None:
            # Reserve space for the upcoming request: the current prompt, the
            # system instructions, and the tool schema. The compaction window
            # is sized so window + prompt + instructions + schema stays within
            # keep_recent_tokens, preventing a compacted window that — combined
            # with a large new prompt — still exceeds the provider limit.
            reservation = self.request_budget_estimator.build_reservation(
                prompt=prompt,
                instructions=self.instructions,
                tool_schemas=self.tool_schema_documents,
                safety_tokens=max(1, self.context_manager.config.soft_token_limit // 100),
            )
            try:
                prepared = await self.context_manager.prepare(
                    history,
                    self.controller.snapshot(),
                    diagnostics,
                    emit,
                    previous_summary=previous_summary,
                    reservation=reservation,
                )
            except Exception as error:
                await emit(RunFailed(str(error)))
                raise attach_partial_outcome(
                    error,
                    PartialRunOutcome(
                        status="failed",
                        message=str(error),
                        usage=asdict(run_usage),
                        plan=self.controller.snapshot(),
                        diagnostics=list(diagnostics),
                        retryable=False,
                    ),
                ) from None
            active_history_input: Sequence[ModelMessage] = prepared.history
            context_estimate = estimate_message_tokens(prepared.history) + reservation.total_tokens
        else:
            active_history_input = history

        async def handle_deferred(_ctx: object, requests: DeferredToolRequests) -> DeferredToolResults | None:
            if not requests.approvals:
                return None
            decisions: dict[str, DeferredToolApprovalResult | bool] = {}
            for call in requests.approvals:
                metadata = self.tool_metadata.get(call.tool_name, {})
                request = ApprovalRequest(
                    call_id=call.tool_call_id,
                    name=call.tool_name,
                    args=call.args_as_dict(),
                    origin=metadata.get("origin", "configured tool"),
                    risk=metadata.get("risk", "external"),
                )
                # The ``approve`` callback owns the user-facing surface: it
                # decides whether to mount an Allow/Deny card (manual mode) or
                # short-circuit (auto mode). We do NOT emit
                # ``ToolApprovalPending`` here — doing so would mount a card
                # even when the caller is about to auto-approve. The callback
                # emits the pending event itself when it actually needs to ask.
                decision = await approve(request)
                approval_log.append(
                    {
                        "call_id": request.call_id,
                        "name": request.name,
                        "approved": decision.approved,
                        "message": decision.message,
                        "mode": _approval_message_field(decision.message, "mode"),
                        "decision_source": _approval_message_field(
                            decision.message,
                            "decision_source",
                        )
                        or ("policy" if decision.message.startswith("auto-approved") else "user"),
                        "origin": request.origin,
                        "risk": request.risk,
                        "args": request.args,
                    }
                )
                if not decision.approved:
                    diagnostics.append(
                        ToolExecutionDiagnostic(
                            call_id=request.call_id,
                            name=request.name,
                            status="denied",
                            error_category="denied",
                            message=decision.message,
                        ).to_dict()
                    )
                await emit(
                    ToolApprovalResolved(
                        call_id=request.call_id,
                        approved=decision.approved,
                        message=decision.message,
                    )
                )
                decisions[call.tool_call_id] = (
                    ToolApproved() if decision.approved else ToolDenied(message=decision.message)
                )
            return requests.build_results(approvals=decisions)

        # Usage limits. We deliberately do NOT pass ``total_tokens_limit``:
        # coding-agent doesn't cap cumulative tokens either. Context growth is
        # handled by :class:`ContextManager`'s auto-compaction, which summarizes
        # old history before the provider's window fills. A hard token wall
        # would halt long agentic loops mid-task.
        limits = UsageLimits(
            request_limit=self.limits.request_count,
            tool_calls_limit=self.limits.tool_calls,
        )
        final_result: AgentRunResultEvent[str] | None = None
        # Per-response text buffer. If a model response produces any tool call,
        # its text is flushed as commentary rather than reaching the final
        # answer widget. ``response_finished_with_tool`` is set when we see a
        # tool result, signalling that the next text we see starts a fresh
        # (likely final) model response.
        response_text_buffer: list[str] = []
        response_has_tool_call = False
        response_finished_with_tool = False
        # Accumulates the final-answer text across the whole run for the partial
        # outcome, so a failed/cancelled run still records what was produced.
        partial_text_parts: list[str] = []
        # Tracks whether any stream event has been emitted in the current
        # attempt. Used by the retry loop to decide whether a mid-stream error
        # can be retried (no events yet) or must propagate (events emitted).
        stream_started = False

        async def _flush_response_text(*, as_final: bool) -> None:
            nonlocal response_text_buffer
            if not response_text_buffer:
                return
            joined = "".join(response_text_buffer)
            response_text_buffer = []
            had_tool = response_has_tool_call
            if joined:
                # Text is streamed speculatively as assistant output because
                # providers don't reveal whether a response will call a tool
                # until the call event arrives. If it does, retract that live
                # segment and replay it once as commentary. Otherwise commit
                # it to the partial/final answer without emitting a duplicate.
                if had_tool and not as_final:
                    await emit(TextRetracted(len(joined)))
                    await emit(CommentaryDelta(joined))
                else:
                    partial_text_parts.append(joined)

        def _build_partial(status: str, message: str, *, retryable: bool) -> PartialRunOutcome:
            """Assemble the partial audit record from accumulated run state."""
            return PartialRunOutcome(
                status=status,
                message=message,
                approvals=list(approval_log),
                usage=_merge_usage(
                    prepared.usage if prepared is not None else {},
                    asdict(run_usage),
                ),
                plan=self.controller.snapshot(),
                diagnostics=list(diagnostics),
                partial_text="".join(partial_text_parts),
                retryable=retryable,
            )

        try:
            # Retry loop for transient provider errors (429, 503, connection
            # resets). We only retry if NO events have been emitted yet — once
            # the stream starts producing content, a retry would duplicate it
            # in the timeline. Mid-stream failures propagate immediately.
            for attempt in range(_RETRY_MAX_ATTEMPTS):
                try:
                    with self.agent.parallel_tool_call_execution_mode("sequential"):
                        async with self.agent.run_stream_events(
                            prompt,
                            message_history=active_history_input,
                            usage_limits=limits,
                            usage=run_usage,
                            capabilities=[
                                HandleDeferredToolCalls(handle_deferred),  # type: ignore[arg-type]
                                RecoverableToolErrors(),
                                InteractiveInputCapability(self.interactive_queue, emit),
                            ],
                        ) as stream:
                            async for event in stream:
                                stream_started = True
                                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                    # Starting a new text part signals the next model
                                    # response. If we just finished a tool, flush the
                                    # previous (commentary) buffer first.
                                    if response_finished_with_tool:
                                        await _flush_response_text(as_final=False)
                                        response_finished_with_tool = False
                                        response_has_tool_call = False
                                    if event.part.content:
                                        if response_has_tool_call:
                                            await emit(CommentaryDelta(event.part.content))
                                        else:
                                            response_text_buffer.append(event.part.content)
                                            await emit(TextDelta(event.part.content))
                                elif isinstance(event, PartDeltaEvent) and isinstance(
                                    event.delta, TextPartDelta
                                ):
                                    if event.delta.content_delta:
                                        if response_has_tool_call:
                                            await emit(CommentaryDelta(event.delta.content_delta))
                                        else:
                                            response_text_buffer.append(event.delta.content_delta)
                                            await emit(TextDelta(event.delta.content_delta))
                                elif isinstance(event, FunctionToolCallEvent):
                                    # Any text accumulated in this response is commentary.
                                    response_has_tool_call = True
                                    await _flush_response_text(as_final=False)
                                    tool_call_count += 1
                                    call_id = event.part.tool_call_id
                                    start_times[call_id] = time.monotonic()
                                    tool_names[call_id] = event.part.tool_name
                                    metadata = self.tool_metadata.get(event.part.tool_name, {})
                                    await emit(
                                        ToolCallStarted(
                                            call_id=call_id,
                                            name=event.part.tool_name,
                                            args=event.part.args_as_dict(),
                                            origin=metadata.get(
                                                "origin",
                                                "control"
                                                if event.part.tool_name
                                                in {"set_plan", "update_step", "report_progress"}
                                                else "configured tool",
                                            ),
                                            risk=metadata.get("risk", "read"),
                                            started_at=start_times[call_id] - started_at,
                                        )
                                    )
                                elif isinstance(event, FunctionToolResultEvent):
                                    part = event.part
                                    call_id = part.tool_call_id
                                    started = start_times.get(call_id, started_at)
                                    elapsed = time.monotonic() - started
                                    outcome = getattr(part, "outcome", "failed")
                                    # Surface the tool's own result text verbatim — the
                                    # model already sees this through pydantic AI's
                                    # normal tool-return plumbing, so no extra
                                    # classification or retry hint is added here. This
                                    # mirrors pi's minimal "feed back, don't editorialise"
                                    # philosophy: trust the model to react to the failure.
                                    raw_content = event.content if event.content is not None else part.content
                                    exit_code, status, error_category = _tool_result_status(
                                        raw_content,
                                        outcome=str(outcome),
                                    )
                                    is_error = status != "success"
                                    content_str = str(raw_content)
                                    preview = content_str[:200]
                                    finished_calls.add(call_id)
                                    diagnostics.append(
                                        ToolExecutionDiagnostic(
                                            call_id=call_id,
                                            name=part.tool_name or "<unknown>",
                                            status=status,
                                            error_category=error_category,
                                            exit_code=exit_code,
                                            elapsed_seconds=elapsed,
                                            message=preview if is_error else None,
                                        ).to_dict()
                                    )
                                    await emit(
                                        ToolCallFinished(
                                            call_id=call_id,
                                            name=part.tool_name or "<unknown>",
                                            result=content_str,
                                            is_error=is_error,
                                            elapsed_seconds=elapsed,
                                            preview=preview,
                                            exit_code=exit_code,
                                        )
                                    )
                                    response_finished_with_tool = True
                                elif isinstance(event, AgentRunResultEvent):
                                    # Final response. Flush whatever text this response
                                    # accumulated as the terminal answer, never as
                                    # commentary (this is the user-facing answer).
                                    await _flush_response_text(as_final=True)
                                    final_result = event
                    break  # stream completed successfully — exit retry loop
                except Exception as retry_error:
                    # Only retry transient errors that occurred BEFORE any event
                    # was emitted. Once we've streamed content, propagate — a
                    # retry would duplicate timeline events.
                    if stream_started or not _is_transient(retry_error) or attempt >= _RETRY_MAX_ATTEMPTS - 1:
                        raise
                    delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                    await asyncio.sleep(delay)
                    continue
            if final_result is None:
                raise RuntimeError("agent stream ended without a final result")
            result = final_result.result
            usage = _merge_usage(
                prepared.usage if prepared is not None else {},
                asdict(run_usage),
            )
            output = str(result.output)
            elapsed_total = time.monotonic() - started_at
            await emit(
                UsageUpdated(
                    usage=usage,
                    request_count=int(usage.get("requests", 0)),
                    tool_call_count=tool_call_count,
                    context_tokens_estimate=context_estimate,
                    elapsed_seconds=elapsed_total,
                )
            )
            await emit(RunCompleted(output, usage))
            # The active history the next run should see is whatever the model
            # actually consumed (possibly compacted) plus the new messages from
            # this run. The full raw history is still preserved by the caller.
            active_history: list[ModelMessage] = [*active_history_input, *result.new_messages()]
            return RunOutcome(
                output=output,
                new_messages=result.new_messages(),
                usage=usage,
                approvals=approval_log,
                plan=self.controller.snapshot(),
                active_history=active_history,
                diagnostics=diagnostics,
                compaction=prepared.compaction if prepared is not None else None,
            )
        except asyncio.CancelledError as error:
            # Flush any buffered text so the user sees partial output before
            # the cancellation message. Then emit the cancellation event.
            await _flush_response_text(as_final=False)
            _append_unfinished_diagnostics(
                diagnostics,
                start_times=start_times,
                tool_names=tool_names,
                finished_calls=finished_calls,
                status="cancelled",
                category="cancelled",
            )
            await emit(RunCancelled())
            # Attach the partial outcome so the caller can persist a faithful
            # (non-empty) cancelled turn instead of an empty record.
            raise attach_partial_outcome(
                error,
                PartialRunOutcome(
                    status="cancelled",
                    message="Run cancelled",
                    approvals=list(approval_log),
                    usage=_merge_usage(
                        prepared.usage if prepared is not None else {},
                        asdict(run_usage),
                    ),
                    plan=self.controller.snapshot(),
                    diagnostics=list(diagnostics),
                    partial_text="".join(partial_text_parts),
                    retryable=False,
                ),
            ) from None
        except IncompleteToolCall as error:
            # Truncated tool call — distinct from a model/prompt failure. The
            # model didn't do anything wrong; the output token budget just ran
            # out mid-arguments. Surface the fix (raise max_tokens) so the user
            # knows this is a config issue, not a prompt issue.
            await _flush_response_text(as_final=False)
            await emit(RunFailed(_friendly_truncation_message(error)))
            raise attach_partial_outcome(
                error,
                _build_partial("failed", _friendly_truncation_message(error), retryable=True),
            ) from None
        except UsageLimitExceeded as error:
            # Budget exhausted — same family: tell the user the limit was hit
            # rather than implying the model misbehaved. We include the
            # configured limits so the user knows exactly what to raise in
            # agent.yaml, and the original error text (which names the
            # specific limit that was breached).
            await _flush_response_text(as_final=False)
            await emit(RunFailed(_friendly_limit_message(error, self.limits)))
            raise attach_partial_outcome(
                error,
                _build_partial("failed", _friendly_limit_message(error, self.limits), retryable=True),
            ) from None
        except Exception as error:
            # Flush buffered text so the user sees whatever the model produced
            # before the failure — a half-streamed answer is better than none.
            await _flush_response_text(as_final=False)
            await emit(RunFailed(str(error)))
            raise attach_partial_outcome(
                error, _build_partial("failed", str(error), retryable=False)
            ) from None
