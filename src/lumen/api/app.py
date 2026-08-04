from __future__ import annotations

# FastAPI consumes decorated route functions through its registry.
# pyright: reportUnusedFunction=false
import asyncio
import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from lumen.application import (
    ApprovalStateError,
    CancelClarification,
    CancelRun,
    ContextControl,
    CreateSession,
    DecideApproval,
    GetBootstrap,
    InvalidStateError,
    InvokePrompt,
    InvokeSkill,
    ListContextSources,
    ListHooks,
    ListMcpPrompts,
    ListSessions,
    QueueRunInput,
    RetryRun,
    RunNotFoundError,
    SelectModel,
    SessionNotFoundError,
    SetApprovalMode,
    SetContextSource,
    StartRun,
    WorkspaceBusyError,
    WorkspaceHost,
    WorkspaceHostError,
)
from lumen.files import search_files

from .schemas import (
    ApprovalBody,
    ControlBody,
    QueueInputBody,
    RetryRunBody,
    StartRunBody,
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
        if isinstance(error, WorkspaceBusyError | ApprovalStateError):
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
            "tools": raw["tools"],
            "mcp": raw["mcp"],
            "skills": raw["skills"],
            "warnings": raw["warnings"],
            "activeRunId": raw["active_run_id"],
        }

    @app.patch("/api/v1/workspace/settings")
    async def update_settings(body: WorkspaceSettingsBody) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        if body.model is not None:
            result = await host.dispatch(SelectModel(body.model))
            changed.update(cast(Any, result).data)
        if body.approval_mode is not None:
            result = await host.dispatch(SetApprovalMode(body.approval_mode, confirmed=body.confirmed))
            changed.update(cast(Any, result).data)
        return {"status": "updated", **changed}

    @app.get("/api/v1/sessions")
    async def list_sessions() -> dict[str, Any]:
        result = await host.dispatch(ListSessions())
        return {
            "sessions": [
                {
                    "sessionId": item.session_id,
                    "createdAt": item.created_at,
                    "modelId": item.model_id,
                    "title": item.title,
                }
                for item in cast(Any, result).sessions
            ]
        }

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session() -> dict[str, str]:
        result = await host.dispatch(CreateSession())
        return {"sessionId": cast(Any, result).session_id}

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

    @app.post("/api/v1/sessions/{session_id}/retry", status_code=202)
    async def retry_run(session_id: str, body: RetryRunBody) -> dict[str, str]:
        result = await host.dispatch(RetryRun(session_id, body.client_request_id))
        return {
            "runId": cast(Any, result).run_id,
            "sessionId": cast(Any, result).session_id,
            "status": cast(Any, result).status,
        }

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

    @app.post("/api/v1/runs/{run_id}/approvals/{call_id}")
    async def decide_approval(run_id: str, call_id: str, body: ApprovalBody) -> dict[str, str]:
        result = await host.dispatch(DecideApproval(run_id, call_id, body.approved))
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
            if (
                not body.name
                or not body.arguments
                or not body.client_request_id
                or not isinstance(raw_arguments, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in raw_arguments.items()
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
