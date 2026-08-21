from __future__ import annotations

# FastAPI consumes decorated route functions through its registry.
# pyright: reportUnusedFunction=false
import asyncio
import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from lumen.application import (
    ApprovalStateError,
    ApproveAgentImport,
    ApproveChildImport,
    ApprovePlan,
    CancelChildRun,
    CancelClarification,
    CancelRun,
    CloseAgent,
    CloseChildRun,
    CommandAcknowledged,
    ConfigurationConflictHostError,
    ContextControl,
    ContinueAgent,
    CreateSession,
    DecideApproval,
    DeleteModelConfiguration,
    DeleteSession,
    DequeueRunInputs,
    EndLiveSession,
    ForkSessionAtTurn,
    GetBootstrap,
    GetConfiguration,
    InterruptAgent,
    InterruptLiveSession,
    InvalidStateError,
    InvokePrompt,
    InvokeSkill,
    ListAgents,
    ListCheckpoints,
    ListChildRuns,
    ListContextSources,
    ListHooks,
    ListMcpPrompts,
    ListSessions,
    QueueRunInput,
    RejectAgentImport,
    RejectChildImport,
    RejectPlan,
    RenameSession,
    RetryRun,
    RunNotFoundError,
    SelectModel,
    SendAgentMessage,
    SessionNotFoundError,
    SetApprovalMode,
    SetCollaborationMode,
    SetContextSource,
    SetSessionArchived,
    SetTranscriptDensity,
    StartLiveSession,
    StartRun,
    UpsertModelConfiguration,
    WaivePlanVerification,
    WorkspaceBusyError,
    WorkspaceHost,
    WorkspaceHostError,
)
from lumen.files import search_files

from .schemas import (
    AgentActionBody,
    ApprovalBody,
    ChildRunActionBody,
    ConfigurationRevisionBody,
    ControlBody,
    ForkSessionBody,
    ModelConfigurationBody,
    PlanReviewBody,
    QueueInputBody,
    RenameSessionBody,
    RetryRunBody,
    SessionSettingsBody,
    StartLiveBody,
    StartRunBody,
    VerificationWaiverBody,
    WorkspaceSettingsBody,
)

_COOKIE = "lumen_web_session"


def _error(code: str, message: str, *, details: Any = None, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details}},
        status_code=status,
    )


def _camel_snapshot(value: Any) -> dict[str, Any]:
    raw = asdict(value)
    timeline = raw.pop("timeline")
    return {
        "sessionId": raw["session_id"],
        "modelId": raw["model_id"],
        "createdAt": raw["created_at"],
        "plan": raw["plan"],
        "timeline": timeline,
        "lastUserInput": raw["last_user_input"],
        "activeRunId": raw["active_run_id"],
        "approvalMode": raw["approval_mode"],
        "collaborationMode": raw["collaboration_mode"],
        "planReviewStatus": raw["plan_review_status"],
        "transcriptDensity": raw["transcript_density"],
        "pendingClarification": raw["pending_clarification"],
        "workProducts": raw["work_products"],
        "pendingEffects": raw["pending_effects"],
        "recoverableEffects": raw["recoverable_effects"],
        "agents": raw["agents"],
        "agentUsage": raw["agent_usage"],
        "liveSessions": raw["live_sessions"],
    }


def _camel_configuration(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": raw["revision"],
        "targetPath": raw["target_path"],
        "editable": raw["editable"],
        "editReason": raw["edit_reason"],
        "exclusive": raw["exclusive"],
        "sources": raw["sources"],
        "warnings": raw["warnings"],
        "defaultModel": raw["default_model"],
        "models": [
            {
                "name": model["name"],
                "id": model["id"],
                "api": model["api"],
                "baseUrl": model["base_url"],
                "apiKeyEnv": model["api_key_env"],
                "settings": model["settings"],
                "context": model["context"],
                "isDefault": model["is_default"],
                "source": model["source"],
                "authKind": model["auth_kind"],
                "authAvailable": model["auth_available"],
            }
            for model in raw["models"]
        ],
        **(
            {"restartRequired": raw["restart_required"]}
            if "restart_required" in raw
            else {}
        ),
    }


def create_web_app(
    host: WorkspaceHost,
    *,
    launch_token: str,
    static_dir: Path | None = None,
    api_only: bool = False,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    session_secret = secrets.token_urlsafe(32)
    token_unused = True
    accepted_hosts = allowed_hosts or {"127.0.0.1", "localhost", "[::1]", "testserver"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await host.open()
        try:
            yield
        finally:
            await host.close()

    app = FastAPI(title="Lumen Web", version="1", lifespan=lifespan)

    @app.middleware("http")
    async def security(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        public = path in {"/api/v1/health", "/auth/exchange"} or (
            path == "/" and request.query_params.get("token") is not None
        )
        if path.startswith("/api/") and not public:
            if request.headers.get("host", "") not in accepted_hosts:
                return _error("invalid_host", "request host is not allowed", status=403)
            cookie = request.cookies.get(_COOKIE, "")
            if not secrets.compare_digest(cookie, session_secret):
                return _error("unauthorized", "authentication required", status=401)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if request.headers.get("origin") != expected_origin:
                    return _error("invalid_origin", "request origin is not allowed", status=403)
        return await call_next(request)

    @app.exception_handler(WorkspaceHostError)
    async def host_error(_request: Request, error: WorkspaceHostError) -> JSONResponse:
        if isinstance(
            error,
            WorkspaceBusyError | ApprovalStateError | ConfigurationConflictHostError,
        ):
            status = 409
        elif isinstance(error, SessionNotFoundError | RunNotFoundError):
            status = 404
        else:
            status = 400
        return _error(error.code, str(error), details=error.details, status=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        return _error("validation_error", "request validation failed", details=error.errors(), status=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if error.status_code == 404 else "http_error"
        return _error(code, str(error.detail), status=error.status_code)

    def exchange_response() -> Response:
        response = Response(status_code=204)
        response.set_cookie(
            _COOKIE,
            session_secret,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    @app.get("/auth/exchange", include_in_schema=False)
    async def exchange(token: str) -> Response:
        nonlocal token_unused
        if not token_unused or not secrets.compare_digest(token, launch_token):
            return _error("invalid_token", "launch token is invalid or already used", status=401)
        token_unused = False
        return exchange_response()

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        result = await host.dispatch(GetBootstrap())
        raw = asdict(result)
        return {
            "agent": raw["agent"],
            "workspace": raw["workspace"],
            "activeModel": raw["active_model"],
            "modelId": raw["model_id"],
            "availableModels": raw["available_models"],
            "approvalMode": raw["approval_mode"],
            "collaborationMode": raw["collaboration_mode"],
            "tools": raw["tools"],
            "mcp": raw["mcp"],
            "skills": raw["skills"],
            "warnings": raw["warnings"],
            "activeRunId": raw["active_run_id"],
            "liveEnabled": raw["live_enabled"],
        }

    @app.patch("/api/v1/workspace/settings")
    async def update_settings(body: WorkspaceSettingsBody) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        if body.model is not None:
            result = await host.dispatch(SelectModel(body.model))
            changed.update(cast(Any, result).data)
        return {"status": "updated", **changed}

    @app.get("/api/v1/capabilities")
    async def capability_inventory() -> dict[str, Any]:
        return host.capabilities()

    @app.get("/api/v1/configuration")
    async def get_configuration() -> dict[str, Any]:
        result = await host.dispatch(GetConfiguration())
        return _camel_configuration(result.data)

    @app.put("/api/v1/configuration/models/{model_name}")
    async def upsert_model_configuration(
        model_name: str,
        body: ModelConfigurationBody,
    ) -> dict[str, Any]:
        definition = body.model_dump(
            mode="json",
            by_alias=False,
            exclude={"expected_revision", "set_default"},
            exclude_none=True,
        )
        result = await host.dispatch(
            UpsertModelConfiguration(
                expected_revision=body.expected_revision,
                name=model_name,
                definition=definition,
                set_default=body.set_default,
            )
        )
        return {"status": result.status, **_camel_configuration(result.data)}

    @app.delete("/api/v1/configuration/models/{model_name}")
    async def delete_model_configuration(
        model_name: str,
        body: ConfigurationRevisionBody,
    ) -> dict[str, Any]:
        result = await host.dispatch(
            DeleteModelConfiguration(
                expected_revision=body.expected_revision,
                name=model_name,
            )
        )
        return {"status": result.status, **_camel_configuration(result.data)}

    @app.patch("/api/v1/sessions/{session_id}/settings")
    async def update_session_settings(session_id: str, body: SessionSettingsBody) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        if body.approval_mode is not None:
            result = await host.dispatch(SetApprovalMode(session_id, body.approval_mode))
            changed.update(cast(Any, result).data)
        if body.collaboration_mode is not None:
            result = await host.dispatch(SetCollaborationMode(session_id, body.collaboration_mode))
            changed.update(cast(Any, result).data)
        if body.transcript_density is not None:
            result = await host.dispatch(SetTranscriptDensity(session_id, body.transcript_density))
            changed.update(cast(Any, result).data)
        return {"status": "updated", **changed}

    @app.get("/api/v1/sessions")
    async def list_sessions(include_archived: bool = False) -> dict[str, Any]:
        result = await host.dispatch(ListSessions(include_archived=include_archived))
        return {
            "sessions": [
                {
                    "sessionId": item.session_id,
                    "createdAt": item.created_at,
                    "modelId": item.model_id,
                    "title": item.title,
                    "archived": item.archived,
                }
                for item in cast(Any, result).sessions
            ]
        }

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session() -> dict[str, str]:
        result = await host.dispatch(CreateSession())
        return {"sessionId": cast(Any, result).session_id}

    @app.patch("/api/v1/sessions/{session_id}")
    async def rename_session(session_id: str, body: RenameSessionBody) -> dict[str, Any]:
        result = await host.dispatch(RenameSession(session_id, body.title))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.post("/api/v1/sessions/{session_id}/archive")
    async def archive_session(session_id: str) -> dict[str, str]:
        result = await host.dispatch(SetSessionArchived(session_id, True))
        return {"status": cast(Any, result).status}

    @app.delete("/api/v1/sessions/{session_id}/archive")
    async def restore_session(session_id: str) -> dict[str, str]:
        result = await host.dispatch(SetSessionArchived(session_id, False))
        return {"status": cast(Any, result).status}

    @app.delete("/api/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, str]:
        result = await host.dispatch(DeleteSession(session_id))
        return {"status": cast(Any, result).status}

    @app.get("/api/v1/sessions/{session_id}")
    async def session_snapshot(session_id: str) -> dict[str, Any]:
        return _camel_snapshot(await host.snapshot(session_id))

    @app.get("/api/v1/sessions/{session_id}/timeline")
    async def session_timeline(session_id: str) -> dict[str, Any]:
        snapshot = await host.snapshot(session_id)
        return {"items": _camel_snapshot(snapshot)["timeline"], "nextCursor": None}

    @app.post("/api/v1/sessions/{session_id}/runs", status_code=202)
    async def start_run(session_id: str, body: StartRunBody) -> dict[str, str]:
        result = await host.dispatch(StartRun(session_id, body.input, body.client_request_id))
        return {
            "runId": cast(Any, result).run_id,
            "sessionId": cast(Any, result).session_id,
            "status": cast(Any, result).status,
        }

    @app.post("/api/v1/sessions/{session_id}/live", status_code=201)
    async def start_live(session_id: str, body: StartLiveBody) -> dict[str, Any]:
        result = await host.dispatch(
            StartLiveSession(session_id, body.client_request_id, body.sdp, body.route)
        )
        return {
            "liveSessionId": result.live_session_id,
            "sessionId": result.session_id,
            "answerSdp": result.answer_sdp,
            "media": result.media,
            "state": result.state,
        }

    @app.get("/api/v1/live/{live_session_id}")
    async def live_snapshot(live_session_id: str) -> dict[str, Any]:
        return host.live_snapshot(live_session_id)

    @app.get("/api/v1/live/{live_session_id}/events")
    async def live_events(request: Request, live_session_id: str) -> StreamingResponse:
        after_raw = request.headers.get("last-event-id") or request.query_params.get("after")
        try:
            after = int(after_raw) if after_raw else None
        except ValueError as error:
            raise InvalidStateError("Last-Event-ID must be an integer") from error
        if after is not None and after < 0:
            raise InvalidStateError("Last-Event-ID cannot be negative")

        async def frames() -> AsyncIterator[str]:
            async for event in host.subscribe_live(live_session_id, after):
                payload = {
                    "version": 1,
                    "sequence": event.sequence,
                    "sessionId": event.session_id,
                    "liveSessionId": event.live_session_id,
                    "type": event.kind.value,
                    "createdAt": event.created_at,
                    "data": event.data,
                }
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
                yield f"id: {event.sequence}\nevent: {event.kind.value}\ndata: {encoded}\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.websocket("/api/v1/live/{live_session_id}/media")
    async def live_media(websocket: WebSocket, live_session_id: str) -> None:
        host_header = websocket.headers.get("host", "")
        cookie = websocket.cookies.get(_COOKIE, "")
        origin = websocket.headers.get("origin", "")
        expected_scheme = "https" if websocket.url.scheme == "wss" else "http"
        expected_origin = f"{expected_scheme}://{host_header}"
        if (
            host_header not in accepted_hosts
            or not secrets.compare_digest(cookie, session_secret)
            or origin != expected_origin
        ):
            await websocket.close(code=1008, reason="Live media authentication failed")
            return
        try:
            snapshot = host.live_snapshot(live_session_id)
        except WorkspaceHostError:
            await websocket.close(code=1008, reason="Live session not found")
            return
        if snapshot.get("media_kind") != "host_websocket":
            await websocket.close(code=1008, reason="Live session does not use Host WebSocket media")
            return

        await websocket.accept()

        async def send_audio() -> None:
            async for audio in host.subscribe_live_audio(live_session_id):
                await websocket.send_bytes(audio)

        sender = asyncio.create_task(send_audio(), name=f"lumen-live-media-send-{live_session_id}")
        try:
            while True:
                audio = await websocket.receive_bytes()
                if not audio:
                    continue
                if len(audio) > 64 * 1024:
                    await websocket.close(code=1009, reason="Live media frame is too large")
                    return
                await host.send_live_audio(live_session_id, audio)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await sender
            with suppress(WorkspaceHostError):
                await host.dispatch(EndLiveSession(live_session_id))

    @app.post("/api/v1/live/{live_session_id}/interrupt")
    async def interrupt_live(live_session_id: str) -> dict[str, Any]:
        result = await host.dispatch(InterruptLiveSession(live_session_id))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.delete("/api/v1/live/{live_session_id}")
    async def end_live(live_session_id: str) -> dict[str, Any]:
        result = await host.dispatch(EndLiveSession(live_session_id))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.post("/api/v1/live/{live_session_id}/approvals/{call_id}")
    async def decide_live_approval(
        live_session_id: str,
        call_id: str,
        body: ApprovalBody,
    ) -> dict[str, str]:
        result = await host.dispatch(DecideApproval(live_session_id, call_id, body.approved, body.scope))
        return {"status": cast(Any, result).status}

    @app.post("/api/v1/sessions/{session_id}/retry", status_code=202)
    async def retry_run(session_id: str, body: RetryRunBody) -> dict[str, str]:
        result = await host.dispatch(RetryRun(session_id, body.client_request_id))
        return {
            "runId": cast(Any, result).run_id,
            "sessionId": cast(Any, result).session_id,
            "status": cast(Any, result).status,
        }

    @app.post("/api/v1/sessions/{session_id}/plan-review", status_code=202)
    async def review_plan(session_id: str, body: PlanReviewBody) -> dict[str, str]:
        if body.action == "approve":
            result = await host.dispatch(ApprovePlan(session_id, body.revision, body.client_request_id))
        else:
            result = await host.dispatch(
                RejectPlan(
                    session_id,
                    body.revision,
                    body.feedback,
                    body.client_request_id,
                )
            )
        return {
            "runId": cast(Any, result).run_id,
            "sessionId": cast(Any, result).session_id,
            "status": cast(Any, result).status,
        }

    @app.get("/api/v1/sessions/{session_id}/children")
    async def list_child_runs(session_id: str) -> dict[str, Any]:
        result = await host.dispatch(ListChildRuns(session_id))
        return {"items": cast(Any, result).data["items"]}

    @app.get("/api/v1/sessions/{session_id}/agents")
    async def list_agents(session_id: str) -> dict[str, Any]:
        result = await host.dispatch(ListAgents(session_id))
        return {"items": cast(Any, result).data["items"]}

    @app.get("/api/v1/sessions/{session_id}/checkpoints")
    async def list_checkpoints(session_id: str) -> dict[str, Any]:
        result = await host.dispatch(ListCheckpoints(session_id))
        return {"items": cast(Any, result).data["items"]}

    @app.post("/api/v1/sessions/{session_id}/fork", status_code=201)
    async def fork_session(session_id: str, body: ForkSessionBody) -> dict[str, str]:
        result = await host.dispatch(ForkSessionAtTurn(session_id, body.through_turn))
        return {"sessionId": result.session_id}

    @app.post("/api/v1/agents/{agent_id}/actions")
    async def agent_action(agent_id: str, body: AgentActionBody) -> dict[str, Any]:
        if body.action == "send_message":
            if not body.message:
                raise InvalidStateError("send_message requires message")
            command: Any = SendAgentMessage(agent_id, body.message)
        elif body.action == "continue":
            if not body.task:
                raise InvalidStateError("continue requires task")
            command = ContinueAgent(agent_id, body.task)
        elif body.action == "interrupt":
            command = InterruptAgent(agent_id)
        elif body.action == "approve_import":
            command = ApproveAgentImport(agent_id)
        elif body.action == "reject_import":
            command = RejectAgentImport(agent_id)
        else:
            command = CloseAgent(agent_id, body.resolution, body.reason)
        result = cast(CommandAcknowledged, await host.dispatch(command))
        return {"status": result.status, **result.data}

    @app.post("/api/v1/children/{child_id}/actions")
    async def child_run_action(child_id: str, body: ChildRunActionBody) -> dict[str, Any]:
        command = {
            "cancel": CancelChildRun(child_id),
            "approve_import": ApproveChildImport(child_id),
            "reject_import": RejectChildImport(child_id),
            "close": CloseChildRun(child_id),
        }[body.action]
        result = await host.dispatch(command)
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.post("/api/v1/sessions/{session_id}/verification-waivers")
    async def waive_verification(session_id: str, body: VerificationWaiverBody) -> dict[str, Any]:
        result = await host.dispatch(WaivePlanVerification(session_id, tuple(body.scope), body.reason))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.get("/api/v1/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        after_raw = request.headers.get("last-event-id") or request.query_params.get("after")
        try:
            after = int(after_raw) if after_raw else None
        except ValueError as error:
            raise InvalidStateError("Last-Event-ID must be an integer") from error
        if after is not None and after < 0:
            raise InvalidStateError("Last-Event-ID cannot be negative")

        async def frames() -> AsyncIterator[str]:
            async for event in host.subscribe(run_id, after):
                payload = {
                    "version": event.version,
                    "sequence": event.sequence,
                    "sessionId": event.session_id,
                    "runId": event.run_id,
                    "type": event.type,
                    "createdAt": event.created_at,
                    "data": event.data,
                }
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
                yield f"id: {event.sequence}\nevent: {event.type}\ndata: {encoded}\n\n"

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, str]:
        result = await host.dispatch(CancelRun(run_id))
        return {"status": cast(Any, result).status}

    @app.post("/api/v1/runs/{run_id}/input")
    async def queue_input(run_id: str, body: QueueInputBody) -> dict[str, Any]:
        result = await host.dispatch(QueueRunInput(run_id, body.text, body.mode))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.post("/api/v1/runs/{run_id}/input/dequeue")
    async def dequeue_input(run_id: str) -> dict[str, Any]:
        result = await host.dispatch(DequeueRunInputs(run_id))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.post("/api/v1/runs/{run_id}/approvals/{call_id}")
    async def decide_approval(run_id: str, call_id: str, body: ApprovalBody) -> dict[str, str]:
        result = await host.dispatch(DecideApproval(run_id, call_id, body.approved, body.scope))
        return {"status": cast(Any, result).status}

    @app.get("/api/v1/files/search")
    async def file_search(q: str = "@", limit: int = 20) -> dict[str, Any]:
        safe_limit = min(max(limit, 1), 50)
        hits = await asyncio.to_thread(
            search_files,
            q if q.startswith("@") else f"@{q}",
            host.resources.workspace,
        )
        return {
            "items": [
                {"path": item.rel_path, "name": item.name, "isDirectory": item.is_dir}
                for item in hits[:safe_limit]
            ]
        }

    @app.get("/api/v1/sessions/{session_id}/context")
    async def session_context(session_id: str) -> dict[str, Any]:
        result = await host.dispatch(ContextControl(session_id, "report"))
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    @app.get("/api/v1/sessions/{session_id}/context/sources")
    async def session_context_sources(session_id: str) -> dict[str, Any]:
        result = await host.dispatch(ListContextSources(session_id))
        return {"items": cast(Any, result).data["items"]}

    @app.get("/api/v1/mcp/prompts")
    async def mcp_prompts() -> dict[str, Any]:
        result = await host.dispatch(ListMcpPrompts())
        return {"items": cast(Any, result).data["items"]}

    @app.get("/api/v1/hooks")
    async def hooks() -> dict[str, Any]:
        result = await host.dispatch(ListHooks())
        return {"items": cast(Any, result).data["items"]}

    @app.post("/api/v1/sessions/{session_id}/controls")
    async def session_control(session_id: str, body: ControlBody) -> dict[str, Any]:
        if body.type == "invoke_skill":
            if not body.name or not body.client_request_id:
                raise InvalidStateError("invoke_skill requires name and clientRequestId")
            result = await host.dispatch(
                InvokeSkill(
                    session_id,
                    body.name,
                    body.arguments,
                    body.client_request_id,
                )
            )
            return {
                "runId": cast(Any, result).run_id,
                "sessionId": cast(Any, result).session_id,
                "status": cast(Any, result).status,
            }
        if body.type == "invoke_prompt":
            raw_arguments = body.payload.get("arguments", {})
            string_arguments = cast(dict[object, object], raw_arguments)
            if (
                not body.name
                or not body.arguments
                or not body.client_request_id
                or not isinstance(raw_arguments, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str) for key, value in string_arguments.items()
                )
            ):
                raise InvalidStateError(
                    "invoke_prompt requires name, display input, string arguments, and clientRequestId"
                )
            result = await host.dispatch(
                InvokePrompt(
                    session_id,
                    body.name,
                    cast(dict[str, str], raw_arguments),
                    body.arguments,
                    body.client_request_id,
                )
            )
            return {
                "runId": cast(Any, result).run_id,
                "sessionId": cast(Any, result).session_id,
                "status": cast(Any, result).status,
            }
        if body.type == "compact_context":
            result = await host.dispatch(ContextControl(session_id, "compact", focus=body.arguments or None))
        elif body.type == "set_context_source":
            kind = str(body.payload.get("kind", ""))
            reference = str(body.payload.get("reference", body.name or ""))
            if kind not in {"skill", "resource"} or not reference:
                raise InvalidStateError("set_context_source requires kind and reference")
            result = await host.dispatch(
                SetContextSource(
                    session_id,
                    cast(Any, kind),
                    reference,
                    bool(body.payload.get("active", True)),
                )
            )
        elif body.type == "cancel_clarification":
            result = await host.dispatch(CancelClarification(session_id))
        else:
            result = await host.dispatch(
                ContextControl(
                    session_id,
                    "memory",
                    action=body.action or "list",
                    payload=body.payload,
                )
            )
        return {"status": cast(Any, result).status, **cast(Any, result).data}

    if not api_only and static_dir is not None:
        index = static_dir / "index.html"

        @app.get("/", include_in_schema=False)
        async def root(request: Request) -> Response:
            token = request.query_params.get("token")
            if token is not None:
                nonlocal token_unused
                if not token_unused or not secrets.compare_digest(token, launch_token):
                    return _error("invalid_token", "launch token is invalid or already used", status=401)
                token_unused = False
                query = [(key, value) for key, value in request.query_params.multi_items() if key != "token"]
                target = f"/?{urlencode(query)}" if query else "/"
                response = RedirectResponse(target, status_code=303)
                response.set_cookie(
                    _COOKIE,
                    session_secret,
                    httponly=True,
                    samesite="strict",
                    secure=False,
                    path="/",
                )
                return response
            if not index.is_file():
                return _error("web_assets_missing", "Lumen Web assets are not built", status=503)
            return Response(index.read_bytes(), media_type="text/html")

        app.mount(
            "/",
            StaticFiles(directory=static_dir, html=True, check_dir=False),
            name="web-static",
        )

    return app
