from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, cast
from uuid import uuid4
from xml.sax.saxutils import escape

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    DeferredToolRequests,
    DeferredToolResults,
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRetry,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    Tool,
    ToolApproved,
    ToolDenied,
    UsageLimits,
)
from pydantic_ai.capabilities import AbstractCapability, HandleDeferredToolCalls
from pydantic_ai.exceptions import IncompleteToolCall, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import DeferredToolApprovalResult
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from lumen.completion import CompletionGate, CompletionPolicy
from lumen.config import LimitsConfig
from lumen.context import (
    AgentRef,
    ContextCommit,
    ContextEngine,
    ContextEnvelope,
    ContextRequest,
    PreviousSummary,
    RuntimeContextSnapshot,
    SessionRef,
    TaskSnapshot,
)
from lumen.context.session_state import PendingClarification
from lumen.events import (
    ApprovalRequest,
    ClarificationRequested,
    CommentaryDelta,
    PlanUpdated,
    RunCancelled,
    RunCompleted,
    RunEvent,
    RunFailed,
    RunStarted,
    RunWaitingForUser,
    TextDelta,
    TextRetracted,
    ToolApprovalResolved,
    ToolCallFinished,
    ToolCallStarted,
    ToolExecutionDiagnostic,
    UsageUpdated,
    WorkProductChanged,
)
from lumen.hooks import HookBus, HookedFunctionToolset, HookedToolset, HookEvent
from lumen.interactive_queue import InteractiveInputCapability, InteractiveMessageQueue
from lumen.plan import EvidenceKind, EvidenceReceipt, PlanState
from lumen.task_control import TaskController
from lumen.tools.execution import RecoverableToolErrors
from lumen.tools.spec import EffectKind
from lumen.work_products.types import WorkProductEvent

EventSink = Callable[[RunEvent], Awaitable[None]]
ClarificationLoader = Callable[[str], PendingClarification | None]
ClarificationSetter = Callable[[str, str, tuple[str, ...], str | None], PendingClarification]
ClarificationClearer = Callable[[str], None]


def _empty_context_documents(_session_id: str) -> tuple[dict[str, object], ...]:
    return ()


class ProviderRequestPreflight(AbstractCapability):
    """Capture and hard-check the actual request at every model step."""

    def __init__(
        self,
        engine: ContextEngine,
        envelope: ContextEnvelope,
        session_id: str,
        output_reserve_tokens: int,
    ) -> None:
        self.engine = engine
        self.envelope = envelope
        self.session_id = session_id
        self.output_reserve_tokens = output_reserve_tokens
        self.step = 0

    async def before_model_request(
        self,
        ctx: object,
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        del ctx
        self.step += 1
        instructions = "\n".join(
            str(getattr(message, "instructions", "") or "")
            for message in request_context.messages
            if getattr(message, "instructions", None)
        )
        schemas = [asdict(tool) for tool in request_context.model_request_parameters.function_tools]
        fitted_messages, snapshot = self.engine.adapt_request_history(
            self.envelope,
            request_context.messages,
            instructions=instructions,
            tool_schemas=schemas,
            output_reserve_tokens=self.output_reserve_tokens,
            session_id=self.session_id,
            model_step=self.step,
        )
        if fitted_messages != request_context.messages:
            request_context.messages[:] = fitted_messages
        self.engine.ensure_request_fits(snapshot)
        return request_context


class ClarificationGate:
    """Per-run blocking clarification state shared by tool and capability."""

    def __init__(self, setter: ClarificationSetter | None) -> None:
        self.setter = setter
        self.session_id = "default"
        self.pending: PendingClarification | None = None
        self.emit: EventSink | None = None

    def start(self, session_id: str, emit: EventSink) -> None:
        self.session_id = session_id
        self.pending = None
        self.emit = emit

    async def request(
        self,
        question: str,
        choices: list[str] | None = None,
        related_plan_step: str | None = None,
    ) -> str:
        question = question.strip()
        if not question or len(question) > 2_000:
            raise ValueError("question must contain 1-2000 characters")
        normalized = tuple(str(item).strip() for item in (choices or []))
        if len(normalized) > 5 or any(not item or len(item) > 200 for item in normalized):
            raise ValueError("choices may contain at most 5 non-empty items of up to 200 characters")
        if self.setter is not None:
            pending = self.setter(self.session_id, question, normalized, related_plan_step)
        else:
            from datetime import UTC, datetime

            pending = PendingClarification(
                id=f"clarify-{uuid4().hex[:12]}",
                question=question,
                choices=normalized,
                related_plan_step=related_plan_step,
                created_at=datetime.now(UTC),
            )
        self.pending = pending
        if self.emit is not None:
            await self.emit(
                ClarificationRequested(
                    pending.id,
                    pending.question,
                    pending.choices,
                    pending.related_plan_step,
                )
            )
        return f"clarification requested: {pending.id}"


class ClarificationCapability(AbstractCapability):
    """Hide and reject tools after a blocking clarification request."""

    def __init__(self, gate: ClarificationGate) -> None:
        self.gate = gate

    async def prepare_tools(self, ctx: object, tool_defs: list[Any]) -> list[Any]:
        del ctx
        return [] if self.gate.pending is not None else tool_defs

    async def before_tool_execute(
        self,
        ctx: object,
        *,
        call: Any,
        tool_def: Any,
        args: Any,
    ) -> Any:
        del ctx, tool_def
        if self.gate.pending is not None and call.tool_name != "request_clarification":
            raise RuntimeError("tool execution is blocked while waiting for user clarification")
        return args


def _recovery_signature(tool_name: str, args: object) -> tuple[str, object]:
    """Return a stable identity and JSON-safe argument snapshot for one call."""

    encoded = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    safe_args = json.loads(encoded)
    digest = hashlib.sha256(f"{tool_name}\n{encoded}".encode()).hexdigest()
    return f"sha256:{digest}", safe_args


def _json_safe_result(result: object) -> object:
    return json.loads(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


class RecoveryReceiptCapability(AbstractCapability):
    """Replay exact successful side-effect calls during an explicit retry.

    Read-only and control tools intentionally bypass this layer. A receipt is
    reusable only when both the tool name and canonical validated arguments
    match, so a changed retry request executes normally.
    """

    def __init__(
        self,
        tool_metadata: Mapping[str, Mapping[str, str]],
        receipts: Sequence[Mapping[str, object]],
    ) -> None:
        self.tool_metadata = tool_metadata
        self.prior = {
            str(receipt.get("signature")): dict(receipt)
            for receipt in receipts
            if receipt.get("signature") and receipt.get("status") == "success"
        }
        self.completed: list[dict[str, object]] = []

    async def wrap_tool_execute(
        self,
        ctx: object,
        *,
        call: Any,
        tool_def: Any,
        args: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        del ctx, tool_def
        name = str(call.tool_name)
        metadata = self.tool_metadata.get(name, {})
        if metadata.get("risk", "external_unknown") == "read" or metadata.get("control") == "true":
            return await handler(args)
        signature, safe_args = _recovery_signature(name, args)
        receipt = self.prior.get(signature)
        if receipt is not None:
            replayed = {**receipt, "replayed": True}
            self.completed.append(replayed)
            return receipt.get("result")
        result = await handler(args)
        self.completed.append(
            {
                "signature": signature,
                "tool_name": name,
                "args": safe_args,
                "result": _json_safe_result(result),
                "status": "success",
                "origin": metadata.get("origin", "unregistered tool"),
                "risk": metadata.get("risk", "external_unknown"),
                "replayed": False,
            }
        )
        return result


def _render_tool_content(content: object) -> str:
    """Render structured tool results consistently for events and the TUI."""

    if isinstance(content, str):
        return content
    if isinstance(content, Mapping | list | tuple):
        try:
            return json.dumps(content, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            pass
    return str(cast(object, content))


@dataclass(frozen=True, slots=True)
class ToolApproval:
    approved: bool
    message: str = "The user denied this tool call."
    remember: bool = False


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ToolApproval]]
ApprovalBatchHandler = Callable[[tuple[ApprovalRequest, ...]], Awaitable[dict[str, ToolApproval]]]


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
When required information is missing and work cannot safely continue, call
request_clarification by itself. Do not call workspace or MCP tools after it.
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
        "link_evidence": controller.link_evidence,
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
    status: str = "completed"
    pending_clarification: PendingClarification | None = None
    recovery_receipts: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    context_fingerprint: str | None = None


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
    pending_clarification: PendingClarification | None = None
    recovery_receipts: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


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
        system_instructions: str | None = None,
        policy_instructions: str = "",
        limits: LimitsConfig,
        tool_metadata: dict[str, dict[str, str]],
        model_settings: ModelSettings | None = None,
        context_engine: ContextEngine | None = None,
        tool_schema_documents: Sequence[dict[str, Any]] = (),
        active_skill_documents: Callable[[str], Sequence[dict[str, object]]] | None = None,
        retrieved_context_documents: Callable[[str], Sequence[dict[str, object]]] | None = None,
        active_work_product_documents: Callable[[str], Sequence[dict[str, object]]] | None = None,
        work_completion_issues: Callable[[str], Sequence[str]] | None = None,
        usage_enricher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        effect_recorder: Callable[..., object] | None = None,
        work_event_drain: Callable[[str], Sequence[WorkProductEvent]] | None = None,
        bind_session_context: Callable[[str], None] | None = None,
        clarification_loader: ClarificationLoader | None = None,
        clarification_setter: ClarificationSetter | None = None,
        clarification_clearer: ClarificationClearer | None = None,
        hooks: HookBus | None = None,
    ) -> None:
        self.limits = limits
        self.tool_metadata = tool_metadata
        # Store the instructions text so the context engine can reserve space
        # for them in the assembly budget (they're sent with every request).
        self.instructions = instructions
        self.system_instructions = system_instructions or instructions
        self.policy_instructions = policy_instructions
        self.controller = TaskController()
        self._completion_policy = CompletionPolicy()
        self._completion_gate = CompletionGate(work_completion_issues)
        self._last_completion_gate_issues: list[str] = []
        self.context_engine = context_engine
        self.interactive_queue = InteractiveMessageQueue()
        self.hooks = hooks
        self._clarification_loader = clarification_loader
        self._clarification_clearer = clarification_clearer
        self._clarification_gate = ClarificationGate(clarification_setter)
        clarification_tool = Tool(
            self._clarification_gate.request,
            name="request_clarification",
            description=(
                "Ask one blocking question when required information is missing. "
                "Call this tool alone; the run will wait for the user's next message."
            ),
            sequential=True,
            requires_approval=False,
            metadata={"origin": "control", "risk": "read", "control": "true"},
        )
        control_tools = [
            _control_tool(name, self.controller)
            for name in ("set_plan", "update_step", "link_evidence", "report_progress")
        ] + [clarification_tool]
        self.tool_schema_documents = [
            *(_tool_schema_document(tool) for tool in [*control_tools, *tools]),
            *tool_schema_documents,
        ]
        self._active_skill_documents: Callable[[str], Sequence[dict[str, object]]] = (
            active_skill_documents or _empty_context_documents
        )
        self._retrieved_context_documents: Callable[[str], Sequence[dict[str, object]]] = (
            retrieved_context_documents or _empty_context_documents
        )
        self._active_work_product_documents: Callable[[str], Sequence[dict[str, object]]] = (
            active_work_product_documents or _empty_context_documents
        )
        self._usage_enricher = usage_enricher
        self._effect_recorder = effect_recorder
        self._work_event_drain = work_event_drain
        self._active_session_id: ContextVar[str] = ContextVar(
            "lumen_runtime_session_id",
            default="default",
        )
        self._bind_session_context = bind_session_context
        runtime_toolsets: list[AbstractToolset[None]] = list(toolsets)
        if hooks is not None and hooks.hooks:
            runtime_toolsets = [HookedToolset(toolset, hooks) for toolset in runtime_toolsets]
        self.agent: Agent[None, str] = Agent(
            model,
            instructions=instructions,
            deps_type=type(None),
            tools=[*control_tools, *tools],
            toolsets=runtime_toolsets,
            model_settings=model_settings,
            tool_timeout=limits.tool_timeout_seconds,
            retries={"output": CompletionPolicy().max_retries},
        )

        def validate_completion(output: str) -> str:
            issues = self._completion_gate_issues()
            self._last_completion_gate_issues = issues
            if issues:
                raise ModelRetry("completion_gate_failed: " + "; ".join(issues))
            return output

        self.agent.output_validator(validate_completion)
        if hooks is not None and hooks.hooks:
            self.agent._function_toolset = HookedFunctionToolset(  # type: ignore[reportPrivateUsage]
                [*control_tools, *tools], hooks
            )

    async def run(
        self,
        prompt: str,
        history: Sequence[ModelMessage],
        emit: EventSink,
        approve: ApprovalHandler,
        approve_batch: ApprovalBatchHandler | None = None,
        *,
        plan: PlanState | None = None,
        previous_summary: PreviousSummary | None = None,
        previous_checkpoint: Any = None,
        compacted_prefix_length: int = 0,
        source_offset: int = 0,
        source_history: Sequence[ModelMessage] = (),
        episode_documents: Sequence[Mapping[str, object]] = (),
        session_id: str | None = None,
        focus: str | None = None,
        force_compaction: bool = False,
        recovery_receipts: Sequence[Mapping[str, object]] = (),
        completion_policy: CompletionPolicy | None = None,
    ) -> RunOutcome:
        await emit(RunStarted(prompt))
        resolved_session_id = session_id or "default"
        self._active_session_id.set(resolved_session_id)
        previous_clarification = (
            self._clarification_loader(resolved_session_id)
            if self._clarification_loader is not None
            else None
        )
        self._clarification_gate.start(resolved_session_id, emit)
        if self._bind_session_context is not None:
            self._bind_session_context(resolved_session_id)
        if self.hooks is not None:
            self.hooks.bind_session(session_id or "default")
            prompt_decision = await self.hooks.dispatch(
                self.hooks.context(HookEvent.USER_PROMPT_SUBMIT, prompt=prompt)
            )
            if not prompt_decision.allow:
                message = prompt_decision.reason or "prompt denied by user_prompt_submit hook"
                await emit(RunFailed(message))
                raise PermissionError(message)
            if prompt_decision.modified_prompt is not None:
                prompt = prompt_decision.modified_prompt
        if previous_clarification is not None:
            prompt = (
                f'<clarification-answer question-id="{escape(previous_clarification.id)}">\n'
                f"{escape(prompt)}\n</clarification-answer>"
            )
        self.controller.start(plan or PlanState(), emit)
        self._completion_policy = completion_policy or CompletionPolicy()
        self._last_completion_gate_issues = []

        approval_log: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        recovery = RecoveryReceiptCapability(self.tool_metadata, recovery_receipts)
        started_at = time.monotonic()
        start_times: dict[str, float] = {}
        tool_names: dict[str, str] = {}
        finished_calls: set[str] = set()
        tool_call_count = 0
        run_usage = RunUsage()
        # ``context_estimate`` is the value surfaced in ``UsageUpdated``; the
        # engine computes it during prepare (no estimator reference here).
        context_estimate = 0

        # The context engine is the single Seam for context assembly: it sizes
        # the request reservation, decides whether to compact, and returns a
        # provider-ready envelope. ``previous_summary`` (from the last compaction
        # in this session) lets the summarizer do an iterative update instead of
        # rebuilding from scratch, preventing summary drift on long sessions.
        envelope: ContextEnvelope | None = None
        if self.context_engine is not None:
            request = ContextRequest(
                session=SessionRef(id=session_id or "default"),
                agent=AgentRef(name="lumen"),
                prompt=prompt,
                task=TaskSnapshot(plan=self.controller.snapshot(), diagnostics=tuple(diagnostics)),
                runtime=RuntimeContextSnapshot(
                    instructions=self.instructions,
                    system_instructions=self.system_instructions,
                    policy_instructions=self.policy_instructions,
                    tool_schema_documents=tuple(self.tool_schema_documents),
                    active_skill_documents=tuple(
                        dict(document) for document in self._active_skill_documents(resolved_session_id)
                    ),
                    retrieved_context_documents=tuple(
                        [
                            *(
                                dict(document)
                                for document in self._retrieved_context_documents(
                                    resolved_session_id
                                )
                            ),
                            *(dict(document) for document in episode_documents),
                        ]
                    ),
                    work_product_documents=tuple(
                        dict(document)
                        for document in self._active_work_product_documents(resolved_session_id)
                    ),
                ),
                history=tuple(history),
                source_history=tuple(source_history),
                previous_summary=previous_summary,
                previous_checkpoint=previous_checkpoint,
                compacted_prefix_length=compacted_prefix_length,
                source_offset=source_offset,
                focus=focus,
                force_compaction=force_compaction,
            )
            try:
                envelope = await self.context_engine.prepare(request, emit)
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
            active_history_input: Sequence[ModelMessage] = list(envelope.messages)
            context_estimate = envelope.budget.used_tokens if envelope.budget is not None else 0
        else:
            active_history_input = history

        async def handle_deferred(_ctx: object, requests: DeferredToolRequests) -> DeferredToolResults | None:
            if not requests.approvals:
                return None
            decisions: dict[str, DeferredToolApprovalResult | bool] = {}
            approval_requests = tuple(
                ApprovalRequest(
                    call_id=call.tool_call_id,
                    name=call.tool_name,
                    args=call.args_as_dict(),
                    origin=self.tool_metadata.get(call.tool_name, {}).get(
                        "origin", "unregistered remote tool"
                    ),
                    risk=self.tool_metadata.get(call.tool_name, {}).get("risk", "external_unknown"),
                )
                for call in requests.approvals
            )
            if len(approval_requests) > 1 and approve_batch is not None:
                resolved = await approve_batch(approval_requests)
            else:
                resolved = {}
                for request in approval_requests:
                    resolved[request.call_id] = await approve(request)
            for request in approval_requests:
                decision = resolved[request.call_id]
                # The ``approve`` callback owns the user-facing surface: it
                # decides whether to mount an Allow/Deny card (manual mode) or
                # short-circuit (auto mode). We do NOT emit
                # ``ToolApprovalPending`` here — doing so would mount a card
                # even when the caller is about to auto-approve. The callback
                # emits the pending event itself when it actually needs to ask.
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
                decisions[request.call_id] = (
                    ToolApproved() if decision.approved else ToolDenied(message=decision.message)
                )
            return requests.build_results(approvals=decisions)

        # Usage limits. We deliberately do NOT pass ``total_tokens_limit``:
        # coding-agent doesn't cap cumulative tokens either. Context growth is
        # handled by the context engine's auto-compaction, which summarizes old
        # history before the provider's window fills. A hard token wall would
        # halt long agentic loops mid-task.
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
        candidate_output_complete = False
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

        async def _discard_retried_output() -> None:
            nonlocal response_text_buffer, candidate_output_complete
            if not candidate_output_complete:
                return
            joined = "".join(response_text_buffer)
            response_text_buffer = []
            candidate_output_complete = False
            if joined:
                await emit(TextRetracted(len(joined)))

        def _build_partial(status: str, message: str, *, retryable: bool) -> PartialRunOutcome:
            """Assemble the partial audit record from accumulated run state."""
            return PartialRunOutcome(
                status=status,
                message=message,
                approvals=list(approval_log),
                usage=_merge_usage(
                    envelope.compaction.usage
                    if envelope is not None and envelope.compaction is not None
                    else {},
                    asdict(run_usage),
                ),
                plan=self.controller.snapshot(),
                diagnostics=list(diagnostics),
                partial_text="".join(partial_text_parts),
                retryable=retryable,
                pending_clarification=self._clarification_gate.pending,
                recovery_receipts=list(recovery.completed),
            )

        try:
            # Retry loop for transient provider errors (429, 503, connection
            # resets). We only retry if NO events have been emitted yet — once
            # the stream starts producing content, a retry would duplicate it
            # in the timeline. Mid-stream failures propagate immediately.
            for attempt in range(_RETRY_MAX_ATTEMPTS):
                try:
                    scheduling = (
                        "sequential" if self.limits.parallel_tool_calls == "sequential" else "parallel"
                    )
                    with self.agent.parallel_tool_call_execution_mode(scheduling):
                        async with self.agent.run_stream_events(
                            prompt,
                            message_history=active_history_input,
                            usage_limits=limits,
                            usage=run_usage,
                            capabilities=[
                                *(
                                    [
                                        ProviderRequestPreflight(
                                            self.context_engine,
                                            cast(ContextEnvelope, envelope),
                                            resolved_session_id,
                                            (
                                                envelope.request_snapshot.output_reserve_tokens
                                                if envelope is not None
                                                and envelope.request_snapshot is not None
                                                else 0
                                            ),
                                        )
                                    ]
                                    if self.context_engine is not None
                                    else []
                                ),
                                ClarificationCapability(self._clarification_gate),
                                recovery,
                                HandleDeferredToolCalls(handle_deferred),  # type: ignore[arg-type]
                                RecoverableToolErrors(),
                                InteractiveInputCapability(self.interactive_queue, emit),
                            ],
                        ) as stream:
                            async for event in stream:
                                stream_started = True
                                if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                    await _discard_retried_output()
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
                                elif (
                                    isinstance(event, PartEndEvent)
                                    and event.next_part_kind == "tool-call"
                                ):
                                    candidate_output_complete = False
                                elif isinstance(event, FunctionToolCallEvent):
                                    await _discard_retried_output()
                                    # Any text accumulated in this response is commentary.
                                    response_has_tool_call = True
                                    await _flush_response_text(as_final=False)
                                    tool_call_count += 1
                                    call_id = event.part.tool_call_id
                                    start_times[call_id] = time.monotonic()
                                    tool_names[call_id] = event.part.tool_name
                                    metadata = self.tool_metadata.get(event.part.tool_name, {})
                                    is_control_tool = event.part.tool_name in {
                                        "set_plan",
                                        "update_step",
                                        "link_evidence",
                                        "report_progress",
                                    }
                                    await emit(
                                        ToolCallStarted(
                                            call_id=call_id,
                                            name=event.part.tool_name,
                                            args=event.part.args_as_dict(),
                                            origin=metadata.get(
                                                "origin",
                                                "control" if is_control_tool else "unregistered tool",
                                            ),
                                            risk=metadata.get(
                                                "risk", "read" if is_control_tool else "external_unknown"
                                            ),
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
                                    content_str = _render_tool_content(raw_content)
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
                                    tool_name = part.tool_name or tool_names.get(call_id, "<unknown>")
                                    metadata = self.tool_metadata.get(tool_name, {})
                                    if (
                                        self._effect_recorder is not None
                                        and tool_name not in {
                                            "write_file",
                                            "edit_file",
                                            "open_work_product",
                                            "inspect_work_product",
                                            "change_work_product",
                                            "restore_work_product",
                                        }
                                        and metadata.get("control") != "true"
                                    ):
                                        try:
                                            effect_kind = EffectKind(
                                                metadata.get("effect", EffectKind.UNKNOWN.value)
                                            )
                                        except ValueError:
                                            effect_kind = EffectKind.UNKNOWN
                                        self._effect_recorder(
                                            tool_name=tool_name,
                                            effect_kind=effect_kind,
                                            success=not is_error and exit_code in {None, 0},
                                            summary=(
                                                f"{tool_name} succeeded"
                                                if not is_error
                                                else f"{tool_name} failed: {preview}"
                                            ),
                                        )
                                    if self._work_event_drain is not None:
                                        for work_event in self._work_event_drain(resolved_session_id):
                                            await emit(WorkProductChanged(**work_event))
                                    if tool_name not in {
                                        "set_plan",
                                        "update_step",
                                        "link_evidence",
                                        "report_progress",
                                        "request_clarification",
                                    }:
                                        kind = (
                                            EvidenceKind.COMMAND
                                            if tool_name in {"run_command", "run_skill_script"}
                                            else EvidenceKind.DIFF
                                            if tool_name in {"write_file", "edit_file"}
                                            else EvidenceKind.TOOL
                                        )
                                        receipt = EvidenceReceipt(
                                            id=f"e{uuid4().hex[:16]}",
                                            kind=kind,
                                            source_id=call_id,
                                            summary=(
                                                f"{tool_name} succeeded"
                                                if not is_error
                                                else f"{tool_name} failed: {preview}"
                                            ),
                                            passed=not is_error and exit_code in {None, 0},
                                            sequence=tool_call_count,
                                        )
                                        self.controller.record_evidence(receipt)
                                        await emit(PlanUpdated(self.controller.snapshot()))
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
                                elif isinstance(event, FinalResultEvent):
                                    candidate_output_complete = event.tool_name is None
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
            provider_usage = asdict(run_usage)
            usage = _merge_usage(
                envelope.compaction.usage if envelope is not None and envelope.compaction is not None else {},
                provider_usage,
            )
            if self.context_engine is not None:
                self.context_engine.observe_provider_usage(resolved_session_id, provider_usage)
            if self._usage_enricher is not None:
                usage = self._usage_enricher(resolved_session_id, usage)
            output = str(result.output)
            if self.hooks is not None:
                await self.hooks.dispatch(
                    self.hooks.context(HookEvent.STOP, tool_result=output, prompt=prompt)
                )
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
            pending_clarification = self._clarification_gate.pending
            if pending_clarification is not None:
                await emit(
                    RunWaitingForUser(
                        pending_clarification.id,
                        pending_clarification.question,
                        pending_clarification.choices,
                    )
                )
                run_status = "waiting_for_user"
            else:
                plan = self.controller.snapshot()
                gate_issues = self._completion_gate_issues(plan)
                if gate_issues:
                    await emit(RunFailed("completion_gate_failed: " + "; ".join(gate_issues)))
                    run_status = "failed"
                else:
                    await emit(RunCompleted(output, usage))
                    run_status = "completed"
                    if previous_clarification is not None and self._clarification_clearer is not None:
                        self._clarification_clearer(resolved_session_id)
            # Commit the run's new messages through the engine: it verifies the
            # envelope fingerprint (idempotent for a repeat) and returns the next
            # active history (the model-consumed envelope plus the new messages).
            # The full raw history is still preserved by the caller via the
            # session repository.
            new_messages = result.new_messages()
            if envelope is not None and self.context_engine is not None:
                transition = await self.context_engine.commit(
                    ContextCommit(
                        session=SessionRef(id=session_id or "default"),
                        envelope_fingerprint=envelope.fingerprint,
                        new_messages=tuple(new_messages),
                    ),
                    emit,
                )
                active_history: list[ModelMessage] = list(transition.active_history)
                compaction = envelope.compaction
            else:
                active_history = [*active_history_input, *new_messages]
                compaction = None
            return RunOutcome(
                output=output,
                new_messages=new_messages,
                usage=usage,
                approvals=approval_log,
                plan=self.controller.snapshot(),
                active_history=active_history,
                diagnostics=diagnostics,
                compaction=compaction,
                status=run_status,
                pending_clarification=pending_clarification,
                recovery_receipts=list(recovery.completed),
                context_fingerprint=envelope.fingerprint if envelope is not None else None,
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
                        envelope.compaction.usage
                        if envelope is not None and envelope.compaction is not None
                        else {},
                        asdict(run_usage),
                    ),
                    plan=self.controller.snapshot(),
                    diagnostics=list(diagnostics),
                    partial_text="".join(partial_text_parts),
                    retryable=False,
                    pending_clarification=self._clarification_gate.pending,
                    recovery_receipts=list(recovery.completed),
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
            if isinstance(error, UnexpectedModelBehavior) and self._last_completion_gate_issues:
                await _discard_retried_output()
            else:
                await _flush_response_text(as_final=False)
            message = str(error)
            if isinstance(error, UnexpectedModelBehavior) and self._last_completion_gate_issues:
                message = "completion_gate_failed: " + "; ".join(
                    self._last_completion_gate_issues
                )
            await emit(RunFailed(message))
            raise attach_partial_outcome(
                error, _build_partial("failed", message, retryable=False)
            ) from None

    def _completion_gate_issues(self, plan: PlanState | None = None) -> list[str]:
        return self._completion_gate.evaluate(
            session_id=self._active_session_id.get(),
            plan=plan or self.controller.snapshot(),
            policy=self._completion_policy,
        )
