"""Workspace-scoped lifecycle, tool bridge, recovery, and persistence for Live."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from lumen.completion import CompletionGate, CompletionPolicy
from lumen.config import LiveConfig
from lumen.events import ApprovalRequest
from lumen.live.events import LiveEventJournal
from lumen.live.protocol import (
    LiveCompletionControl,
    LiveProviderCommand,
    LiveProviderEvent,
    LiveProviderEventKind,
    LiveProviderOpenRequest,
    LiveSessionSpec,
)
from lumen.live.router import (
    LiveProviderRouter,
    LiveRouteRequirements,
    RoutedLiveConnection,
)
from lumen.live.types import (
    LiveActionResult,
    LiveActivityState,
    LiveConnectionState,
    LiveConnectRequest,
    LiveConnectResult,
    LiveEvent,
    LiveEventKind,
    LiveSessionRef,
    LiveSessionState,
    LiveUsage,
    utc_now,
)
from lumen.plan import PlanState
from lumen.sessions import SessionRepository
from lumen.tools.gateway import (
    CapabilityApproval,
    CapabilityGateway,
    CapabilityInvocation,
)

LiveApprovalHandler = Callable[[str, str, ApprovalRequest], Awaitable[CapabilityApproval]]
LiveExecutionAcquire = Callable[[str, str], Awaitable[bool]]
LiveExecutionRelease = Callable[[str], Awaitable[None]]
ContextDocuments = Callable[[str], Sequence[Mapping[str, object]]]
PlanProvider = Callable[[str], PlanState]
SessionBinder = Callable[[str, str], None]


@dataclass(slots=True)
class _LiveRecord:
    state: LiveSessionState
    routed: RoutedLiveConnection | None
    journal: LiveEventJournal
    client_request_id: str
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    response_ids_persisted: set[str] = field(default_factory=set[str])
    transcript_parts: list[str] = field(default_factory=list[str])
    rollover_task: asyncio.Task[None] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    audio_queue: asyncio.Queue[bytes | None] = field(
        default_factory=lambda: asyncio.Queue[bytes | None](maxsize=64)
    )


class LiveSessionManager:
    """Deep Module owning active Realtime sessions behind a small Host Interface."""

    def __init__(
        self,
        *,
        config: LiveConfig,
        router: LiveProviderRouter,
        repository: SessionRepository,
        capability_gateway: CapabilityGateway,
        completion_gate: CompletionGate,
        plan_provider: PlanProvider,
        context_documents: ContextDocuments,
        bind_session: SessionBinder | None = None,
    ) -> None:
        self.config = config
        self._router = router
        self._repository = repository
        self._gateway = capability_gateway
        self._completion_gate = completion_gate
        self._plan_provider = plan_provider
        self._context_documents = context_documents
        self._bind_session = bind_session
        self._records: dict[str, _LiveRecord] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._approval_handler: LiveApprovalHandler | None = None
        self._execution_acquire: LiveExecutionAcquire | None = None
        self._execution_release: LiveExecutionRelease | None = None
        self._lock = asyncio.Lock()
        self._recovered_sessions: set[str] = set()

    def bind_approval_handler(self, handler: LiveApprovalHandler) -> None:
        self._approval_handler = handler

    def bind_execution_lease(
        self,
        acquire: LiveExecutionAcquire,
        release: LiveExecutionRelease,
    ) -> None:
        self._execution_acquire = acquire
        self._execution_release = release

    async def connect(self, request: LiveConnectRequest) -> LiveConnectResult:
        if not self.config.enabled:
            raise RuntimeError("Realtime voice is disabled")
        self._repository.load(request.session_id)
        request_key = (request.session_id, request.client_request_id)
        prior: _LiveRecord | None = None
        record: _LiveRecord | None = None
        async with self._lock:
            prior_id = self._requests.get(request_key)
            if prior_id is not None:
                prior = self._records[prior_id]
            else:
                active_count = sum(
                    item.state.connection not in {LiveConnectionState.CLOSED, LiveConnectionState.FAILED}
                    for item in self._records.values()
                    if item.state.ref.session_id == request.session_id
                )
                if active_count >= self.config.max_sessions:
                    raise RuntimeError("maximum active Live sessions reached")
                live_id = f"live-{uuid4().hex}"
                configured_route = self.config.routes.get(
                    request.route or self.config.default_route or ""
                )
                state = LiveSessionState(
                    ref=LiveSessionRef(
                        id=live_id,
                        session_id=request.session_id,
                        provider=(
                            configured_route.provider
                            if configured_route is not None
                            else self.config.provider
                        ),
                    ),
                    model=(configured_route.model if configured_route is not None else self.config.model),
                    voice=(configured_route.voice if configured_route is not None else self.config.voice),
                    route=(request.route or self.config.default_route),
                )
                record = _LiveRecord(
                    state=state,
                    routed=None,
                    journal=LiveEventJournal(
                        live_session_id=live_id,
                        session_id=request.session_id,
                    ),
                    client_request_id=request.client_request_id,
                )
                self._records[live_id] = record
                self._requests[request_key] = live_id
        if prior is not None:
            await prior.ready.wait()
            if prior.routed is None or prior.state.connection is not LiveConnectionState.ACTIVE:
                raise RuntimeError(prior.state.error or "Realtime call creation failed")
            handshake = prior.routed.connection.handshake
            return LiveConnectResult(
                live_session_id=prior.state.ref.id,
                handshake=handshake,
                answer_sdp=handshake.answer_sdp,
                state=prior.state,
            )
        assert record is not None
        live_id = record.state.ref.id
        self._persist(record)
        await record.journal.append(LiveEventKind.SESSION_CREATED)

        try:
            self._update(record, connection=LiveConnectionState.CONNECTING)
            routed = await self._router.open(
                LiveProviderOpenRequest(
                    live_session_id=live_id,
                    session_id=request.session_id,
                    browser_offer_sdp=request.sdp,
                    spec=self._session_spec(request.session_id),
                ),
                lambda event: self._on_provider_event(live_id, event),
                route_name=request.route,
                requirements=LiveRouteRequirements(
                    function_calling=True,
                    interruption=True,
                    input_transcription=self.config.input_transcription.enabled,
                    server_tool_authority=True,
                    strict_completion=self.config.strict_completion,
                ),
            )
            record.routed = routed
            snapshot = routed.snapshot
            self._update(
                record,
                ref=record.state.ref.model_copy(update={"provider": snapshot.provider}),
                model=snapshot.model,
                voice=snapshot.voice or record.state.voice,
                route=snapshot.route,
                region=snapshot.region,
                media_kind=snapshot.media.value,
                completion_control=snapshot.completion_control.value,
                connection=LiveConnectionState.ACTIVE,
                activity=LiveActivityState.LISTENING,
            )
            await record.journal.append(LiveEventKind.SESSION_CONNECTED)
            record.rollover_task = asyncio.create_task(
                self._request_rollover(record),
                name=f"lumen-live-rollover-{live_id}",
            )
            record.ready.set()
            return LiveConnectResult(
                live_session_id=live_id,
                handshake=routed.connection.handshake,
                answer_sdp=routed.connection.handshake.answer_sdp,
                state=record.state,
            )
        except Exception as error:
            if record.routed is not None:
                with suppress(Exception):
                    await record.routed.connection.close()
            self._update(
                record,
                connection=LiveConnectionState.FAILED,
                error=f"{type(error).__name__}: {error}",
            )
            await record.journal.append(
                LiveEventKind.SESSION_FAILED,
                {"message": record.state.error},
            )
            record.ready.set()
            await record.journal.close()
            raise
        except asyncio.CancelledError:
            if record.routed is not None:
                with suppress(Exception):
                    await record.routed.connection.close()
            self._update(
                record,
                connection=(
                    LiveConnectionState.RECONCILIATION_REQUIRED
                    if record.routed is not None
                    else LiveConnectionState.FAILED
                ),
                error="Realtime call creation was interrupted",
            )
            await record.journal.append(
                LiveEventKind.RECONCILIATION_REQUIRED,
                {"message": record.state.error},
            )
            record.ready.set()
            await record.journal.close()
            raise

    def snapshot(self, live_session_id: str) -> LiveSessionState:
        return self._record(live_session_id).state

    async def subscribe(
        self,
        live_session_id: str,
        after_sequence: int | None = None,
    ) -> AsyncIterator[LiveEvent]:
        record = self._record(live_session_id)
        async for event in record.journal.subscribe(after_sequence):
            yield event

    async def interrupt(self, live_session_id: str) -> LiveActionResult:
        record = self._record(live_session_id)
        if record.routed is not None:
            await record.routed.connection.send(LiveProviderCommand.cancel_response())
        self._update(record, activity=LiveActivityState.INTERRUPTED)
        await record.journal.append(LiveEventKind.RESPONSE_INTERRUPTED)
        self._update(record, activity=LiveActivityState.LISTENING)
        return LiveActionResult(state=record.state)

    async def end(self, live_session_id: str) -> LiveActionResult:
        record = self._record(live_session_id)
        if record.state.connection is LiveConnectionState.CLOSED:
            return LiveActionResult(state=record.state)
        self._update(record, connection=LiveConnectionState.CLOSING)
        if record.rollover_task is not None and record.rollover_task is not asyncio.current_task():
            record.rollover_task.cancel()
            with suppress(asyncio.CancelledError):
                await record.rollover_task
        record.rollover_task = None
        for task in tuple(record.tool_tasks):
            task.cancel()
        for task in tuple(record.tool_tasks):
            with suppress(asyncio.CancelledError):
                await task
        if record.routed is not None:
            with suppress(Exception):
                await record.routed.connection.close()
        # End must not wait for an absent/slow media consumer. Discard stale
        # playback and publish the sentinel without awaiting queue capacity.
        while not record.audio_queue.empty():
            record.audio_queue.get_nowait()
        record.audio_queue.put_nowait(None)
        self._update(
            record,
            connection=LiveConnectionState.CLOSED,
            activity=LiveActivityState.IDLE,
        )
        await record.journal.append(LiveEventKind.SESSION_ENDED)
        await record.journal.close()
        return LiveActionResult(state=record.state)

    async def _request_rollover(self, record: _LiveRecord) -> None:
        """Ask the browser for a fresh WebRTC call before provider expiry.

        A new call requires a new browser SDP offer, so the server cannot roll
        it over invisibly.  The durable event lets every Web client use the
        same safe reconnect path without exposing provider credentials.
        """

        await asyncio.sleep(self.config.rollover_seconds)
        if record.state.connection is not LiveConnectionState.ACTIVE:
            return
        self._update(record, connection=LiveConnectionState.RECONNECTING)
        await record.journal.append(
            LiveEventKind.SESSION_RECONNECTING,
            {"reason": "proactive_rollover"},
        )

    async def close(self) -> None:
        for live_id in tuple(self._records):
            with suppress(Exception):
                await self.end(live_id)
        await self._router.close()

    async def send_audio(self, live_session_id: str, audio: bytes) -> None:
        record = self._record(live_session_id)
        if record.routed is None or record.state.connection is not LiveConnectionState.ACTIVE:
            raise RuntimeError("Live media is not active")
        await record.routed.connection.send_audio(audio)

    async def subscribe_audio(self, live_session_id: str) -> AsyncIterator[bytes]:
        record = self._record(live_session_id)
        while True:
            audio = await record.audio_queue.get()
            if audio is None:
                return
            yield audio

    def states_for(self, session_id: str) -> tuple[LiveSessionState, ...]:
        return tuple(item.state for item in self._records.values() if item.state.ref.session_id == session_id)

    async def _on_provider_event(self, live_id: str, event: LiveProviderEvent) -> None:
        record = self._record(live_id)
        if event.kind is LiveProviderEventKind.TRANSPORT_ERROR:
            message = event.message or "Realtime provider error"
            self._update(
                record,
                connection=LiveConnectionState.RECONCILIATION_REQUIRED,
                error=message,
            )
            await record.journal.append(
                LiveEventKind.RECONCILIATION_REQUIRED,
                {"message": message},
            )
            return
        if event.kind is LiveProviderEventKind.SPEECH_STARTED:
            self._update(record, activity=LiveActivityState.USER_SPEAKING)
            await record.journal.append(LiveEventKind.SPEECH_STARTED)
            return
        if event.kind is LiveProviderEventKind.SPEECH_STOPPED:
            self._update(record, activity=LiveActivityState.PROCESSING)
            await record.journal.append(LiveEventKind.SPEECH_STOPPED)
            return
        if event.kind is LiveProviderEventKind.INPUT_TRANSCRIPT_DELTA:
            await record.journal.append(
                LiveEventKind.INPUT_TRANSCRIPT_DELTA,
                {"delta": event.delta or ""},
            )
            return
        if event.kind is LiveProviderEventKind.INPUT_TRANSCRIPT_COMPLETED:
            transcript = (event.transcript or "").strip()
            self._update(record, last_user_transcript=transcript or None)
            await record.journal.append(
                LiveEventKind.INPUT_TRANSCRIPT_COMPLETED,
                {"transcript": transcript},
            )
            return
        if event.kind is LiveProviderEventKind.RESPONSE_STARTED:
            self._update(record, activity=LiveActivityState.PROCESSING)
            await record.journal.append(LiveEventKind.RESPONSE_STARTED)
            return
        if event.kind is LiveProviderEventKind.RESPONSE_AUDIO_DELTA:
            if record.state.activity is not LiveActivityState.ASSISTANT_SPEAKING:
                self._update(record, activity=LiveActivityState.ASSISTANT_SPEAKING)
                await record.journal.append(LiveEventKind.RESPONSE_AUDIO_STARTED)
            if event.audio:
                # Media must not block the provider's control/tool events.
                # Prefer fresh audio to an unbounded backlog for slow clients.
                if record.audio_queue.full():
                    record.audio_queue.get_nowait()
                record.audio_queue.put_nowait(event.audio)
            return
        if event.kind is LiveProviderEventKind.RESPONSE_TRANSCRIPT_DELTA:
            delta = event.delta or ""
            record.transcript_parts.append(delta)
            await record.journal.append(
                LiveEventKind.RESPONSE_TRANSCRIPT_DELTA,
                {"delta": delta},
            )
            return
        if event.kind is LiveProviderEventKind.TOOL_CALL_READY:
            task = asyncio.create_task(
                self._handle_tool_call(record, event),
                name=f"lumen-live-tool-{event.call_id or 'unknown'}",
            )
            record.tool_tasks.add(task)
            task.add_done_callback(record.tool_tasks.discard)
            return
        if event.kind is LiveProviderEventKind.RESPONSE_COMPLETED:
            await self._handle_response_done(record, event)

    async def _handle_tool_call(self, record: _LiveRecord, event: LiveProviderEvent) -> None:
        call_id = (event.call_id or "").strip()
        name = (event.name or "").strip()
        if not call_id or not name or record.routed is None:
            return
        if event.arguments is None:
            await self._send_function_output(
                record,
                call_id,
                {"ok": False, "error": "invalid function arguments: expected a JSON object"},
            )
            return
        arguments = event.arguments

        async with record.tool_lock:
            acquired = True
            if self._execution_acquire is not None:
                acquired = await self._execution_acquire(
                    record.state.ref.session_id,
                    record.state.ref.id,
                )
            if not acquired:
                await self._send_function_output(
                    record,
                    call_id,
                    {
                        "ok": False,
                        "error": "workspace_busy: another root execution is active; retry later",
                    },
                )
                await record.routed.connection.send(
                    LiveProviderCommand.continue_response(required_tool=True)
                )
                return
            try:
                await self._execute_tool_call(record, call_id, name, arguments)
            finally:
                if self._execution_release is not None:
                    await self._execution_release(record.state.ref.id)

    async def _execute_tool_call(
        self,
        record: _LiveRecord,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        if name == "complete_live_turn":
            await self._complete_turn(record, call_id, arguments)
            return
        self._update(
            record,
            activity=(
                LiveActivityState.APPROVAL_PENDING
                if self._requires_approval(name)
                else LiveActivityState.TOOL_RUNNING
            ),
            pending_call_ids=tuple(dict.fromkeys((*record.state.pending_call_ids, call_id))),
        )

        await record.journal.append(
            LiveEventKind.TOOL_STARTED,
            {"call_id": call_id, "name": name, "arguments": arguments},
        )
        if self._bind_session is not None:
            self._bind_session(record.state.ref.session_id, record.state.ref.id)

        async def approve(request: ApprovalRequest) -> CapabilityApproval:
            await record.journal.append(
                LiveEventKind.TOOL_APPROVAL_PENDING,
                {
                    "call_id": request.call_id,
                    "name": request.name,
                    "arguments": request.args,
                    "origin": request.origin,
                    "risk": request.risk,
                },
            )
            if self._approval_handler is None:
                return CapabilityApproval(False, "no Live approval handler is available")
            return await self._approval_handler(
                record.state.ref.session_id,
                record.state.ref.id,
                request,
            )

        result = await self._gateway.invoke(
            CapabilityInvocation(
                execution_id=record.state.ref.id,
                provider_call_id=call_id,
                name=name,
                arguments=arguments,
            ),
            approve=approve,
        )
        pending = tuple(item for item in record.state.pending_call_ids if item != call_id)
        self._update(record, activity=LiveActivityState.PROCESSING, pending_call_ids=pending)
        payload = result.model_dump(mode="json")
        await record.journal.append(
            LiveEventKind.TOOL_FINISHED,
            {"call_id": call_id, "name": name, "result": payload},
        )
        await self._send_function_output(record, call_id, payload)
        if record.routed is not None:
            await record.routed.connection.send(
                LiveProviderCommand.continue_response(
                    required_tool=True if self._uses_native_completion(record) else None
                )
            )

    def recover_session(self, session_id: str) -> None:
        """Safely classify calls that could not survive a process restart."""

        if session_id in self._recovered_sessions:
            return
        loaded = self._repository.load(session_id)
        terminal = {
            LiveConnectionState.CLOSED,
            LiveConnectionState.FAILED,
            LiveConnectionState.RECONCILIATION_REQUIRED,
        }
        for state in loaded.live_state.sessions:
            if state.connection in terminal:
                continue
            if state.pending_call_ids:
                connection = LiveConnectionState.RECONCILIATION_REQUIRED
                error = "Live call ended during an unconfirmed capability execution"
            else:
                connection = LiveConnectionState.CLOSED
                error = "Live transport does not survive restart; start a new voice session"
            self._repository.append_live_session(
                session_id,
                state.model_copy(
                    update={
                        "connection": connection,
                        "activity": LiveActivityState.IDLE,
                        "error": error,
                        "updated_at": utc_now(),
                    }
                ),
            )
        self._recovered_sessions.add(session_id)

    async def _complete_turn(
        self,
        record: _LiveRecord,
        call_id: str,
        arguments: dict[str, Any],
    ) -> None:
        answer = str(arguments.get("answer", "")).strip()
        if not answer:
            await self._send_function_output(
                record,
                call_id,
                {"ok": False, "issues": ["answer must not be empty"]},
            )
            return
        issues = self._completion_gate.evaluate(
            session_id=record.state.ref.session_id,
            plan=self._plan_provider(record.state.ref.session_id),
            policy=CompletionPolicy(),
        )
        if issues:
            await self._send_function_output(record, call_id, {"ok": False, "issues": issues})
            if record.routed is not None:
                await record.routed.connection.send(
                    LiveProviderCommand.continue_response(required_tool=True)
                )
            return
        self._update(record, last_assistant_transcript=answer)
        await self._send_function_output(record, call_id, {"ok": True})
        if record.routed is not None:
            await record.routed.connection.send(LiveProviderCommand.speak_approved(answer))

    async def _send_function_output(
        self,
        record: _LiveRecord,
        call_id: str,
        output: Mapping[str, Any],
    ) -> None:
        if record.routed is None:
            return
        await record.routed.connection.send(
            LiveProviderCommand.submit_tool_result(call_id, dict(output))
        )

    async def _handle_response_done(self, record: _LiveRecord, event: LiveProviderEvent) -> None:
        response_id = event.response_id or event.event_id or ""
        if event.usage is not None:
            usage = self._usage(event.usage)
            self._update(record, usage=usage)
            await record.journal.append(
                LiveEventKind.USAGE_UPDATED,
                usage.model_dump(mode="json"),
            )
        status = event.response_status or "completed"
        if status not in {"completed", "incomplete", "cancelled", "failed"}:
            status = "failed"
        transcript = "".join(record.transcript_parts).strip()
        record.transcript_parts.clear()
        if transcript:
            self._update(record, last_assistant_transcript=transcript)
        if status == "completed":
            approved_answer: str | None = None
            if self._uses_host_gated_completion(record) and transcript:
                issues = self._completion_gate.evaluate(
                    session_id=record.state.ref.session_id,
                    plan=self._plan_provider(record.state.ref.session_id),
                    policy=CompletionPolicy(),
                )
                if issues:
                    self._update(record, activity=LiveActivityState.LISTENING)
                    await record.journal.append(
                        LiveEventKind.COMPLETION_BLOCKED,
                        {"response_id": response_id, "issues": issues},
                    )
                    return
                approved_answer = transcript
            self._update(
                record,
                activity=LiveActivityState.LISTENING,
                turn_count=record.state.turn_count + 1,
            )
            await record.journal.append(
                LiveEventKind.RESPONSE_COMPLETED,
                {"response_id": response_id, "transcript": transcript},
            )
            self._persist_turn(record, response_id)
            if approved_answer is not None:
                await record.journal.append(
                    LiveEventKind.RESPONSE_APPROVED,
                    {"response_id": response_id, "answer": approved_answer},
                )
        else:
            self._update(record, activity=LiveActivityState.LISTENING)
            await record.journal.append(
                LiveEventKind.RESPONSE_INTERRUPTED,
                {"response_id": response_id, "status": status},
            )

    def _persist_turn(self, record: _LiveRecord, response_id: str) -> None:
        if not response_id or response_id in record.response_ids_persisted:
            return
        user = (record.state.last_user_transcript or "").strip()
        assistant = (record.state.last_assistant_transcript or "").strip()
        if not user or not assistant:
            return
        messages = [
            ModelRequest(parts=[UserPromptPart(content=user)]),
            ModelResponse(parts=[TextPart(content=assistant)]),
        ]
        self._repository.append_turn(
            record.state.ref.session_id,
            user_input=user,
            messages=messages,
            approvals=[],
            usage=record.state.usage.model_dump(mode="json"),
            status="completed",
            plan=self._plan_provider(record.state.ref.session_id),
            channel="voice",
            interaction_id=record.state.ref.id,
            input_provenance=f"{record.state.ref.provider}_realtime_transcription",
            provider_item_ids=[response_id],
            live_metadata={
                "route": record.state.route,
                "provider": record.state.ref.provider,
                "region": record.state.region,
                "model": record.state.model,
                "voice": record.state.voice,
                "media_kind": record.state.media_kind,
                "completion_control": record.state.completion_control,
            },
        )
        record.response_ids_persisted.add(response_id)

    def _session_spec(self, session_id: str) -> LiveSessionSpec:
        tools = [
            {
                "type": "function",
                "name": item.name,
                "description": item.description,
                "parameters": item.parameters,
            }
            for item in self._gateway.catalog()
        ]
        if self.config.strict_completion:
            tools.append(
                {
                    "type": "function",
                    "name": "complete_live_turn",
                    "description": (
                        "完成全部工作和验证后, 请求播报最终答案。"
                        "只有通过 Lumen 的完成门禁后才会播报。"
                    ),
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "answer": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["answer"],
                    },
                }
            )
        turn_detection: dict[str, Any] = {
            "type": self.config.turn_detection.type,
            "eagerness": self.config.turn_detection.eagerness,
            "interrupt_response": self.config.turn_detection.interrupt_response,
        }
        transcription: dict[str, Any] | None = None
        if self.config.input_transcription.enabled:
            transcription = {}
            if self.config.input_transcription.model:
                transcription["model"] = self.config.input_transcription.model
            if self.config.input_transcription.language:
                transcription["language"] = self.config.input_transcription.language
        instructions = self._instructions(session_id)
        return LiveSessionSpec(
            instructions=instructions,
            tools=tuple(tools),
            voice=self.config.voice,
            turn_detection=turn_detection,
            input_transcription=transcription,
            reasoning_effort=self.config.reasoning_effort,
            strict_completion=self.config.strict_completion,
        )

    def _instructions(self, session_id: str) -> str:
        documents = list(self._context_documents(session_id))[-self.config.max_context_items :]
        context = json.dumps(documents, ensure_ascii=False, default=str)
        strict = (
            "最终答案受 Lumen 完成策略控制。使用当前实时 Provider 声明的完成协议, 不得绕过。"
            if self.config.strict_completion
            else "工具能提高正确性时使用工具, 并如实报告失败。"
        )
        return (
            "你是实时语音对话中的 Lumen。使用用户的语言自然回答, 并保持口语回复简洁。"
            "工具执行、审批和项目状态由 Lumen 控制。"
            f"{strict}\n\n规范 Session 上下文:\n{context}"
        )

    def _requires_approval(self, name: str) -> bool:
        return next(
            (item.requires_approval for item in self._gateway.catalog() if item.name == name),
            False,
        )

    @staticmethod
    def _uses_native_completion(record: _LiveRecord) -> bool:
        return record.state.completion_control == LiveCompletionControl.NATIVE_REQUIRED_TOOL.value

    @staticmethod
    def _uses_host_gated_completion(record: _LiveRecord) -> bool:
        return record.state.completion_control == LiveCompletionControl.HOST_GATED_SYNTHESIS.value

    @staticmethod
    def _usage(raw: Mapping[str, Any]) -> LiveUsage:
        return LiveUsage(
            input_tokens=int(raw.get("input_tokens", 0) or 0),
            output_tokens=int(raw.get("output_tokens", 0) or 0),
            total_tokens=int(raw.get("total_tokens", 0) or 0),
            details={
                str(key): value
                for key, value in raw.items()
                if key not in {"input_tokens", "output_tokens", "total_tokens"}
            },
        )

    def _update(self, record: _LiveRecord, **updates: Any) -> None:
        updates["updated_at"] = utc_now()
        record.state = record.state.model_copy(update=updates)
        self._persist(record)

    def _persist(self, record: _LiveRecord) -> None:
        self._repository.append_live_session(record.state.ref.session_id, record.state)

    def _record(self, live_session_id: str) -> _LiveRecord:
        try:
            return self._records[live_session_id]
        except KeyError as error:
            raise KeyError(f"Live session not found: {live_session_id}") from error


__all__ = ["LiveApprovalHandler", "LiveSessionManager"]
