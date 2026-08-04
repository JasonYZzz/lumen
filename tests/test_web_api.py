from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.api import create_web_app
from lumen.application import WorkspaceHost
from lumen.config import LimitsConfig
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository


class ApiResources:
    def __init__(self, root: Path) -> None:
        self.rendered_prompts: list[tuple[str, dict[str, str]]] = []

        async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
            yield "hello from api"

        self.workspace = root
        self.session_repository = SessionRepository(root / "sessions")
        self.runtime = AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        )
        self.config = SimpleNamespace(
            agent=SimpleNamespace(name="api-agent"),
            permissions=SimpleNamespace(default_mode="manual"),
        )
        self.tool_metadata: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []
        self.skills: list[object] = []

    async def open(self) -> ApiResources:
        return self

    async def close(self) -> None:
        return None

    def active_model_config(self) -> SimpleNamespace:
        return SimpleNamespace(id="test-model")

    def active_model_name(self) -> str:
        return "test"

    def available_models(self) -> list[str]:
        return ["test"]

    def mcp_summary(self) -> list[dict[str, object]]:
        return []

    def context_source_summary(self, _session_id: str) -> list[dict[str, str]]:
        return [
            {
                "kind": "resource",
                "reference": "docs:guide",
                "revision": "rev-1",
                "status": "available",
            }
        ]

    def mcp_prompt_summary(self) -> list[dict[str, object]]:
        return [
            {
                "reference": "docs:summarize",
                "server": "docs",
                "name": "summarize",
                "description": "Summarize a document",
                "arguments": ("topic",),
            }
        ]

    async def render_mcp_prompt(self, reference: str, arguments: dict[str, str]) -> str:
        self.rendered_prompts.append((reference, arguments))
        return f"Rendered {reference} for {arguments['topic']}"

    def hook_summary(self) -> list[dict[str, object]]:
        return [
            {
                "event": "before_tool",
                "matcher": "write_*",
                "runner": "guard.py",
                "deny_count": 2,
                "last_triggered": "2026-08-04T10:00:00Z",
            }
        ]

    def summary(self) -> dict[str, object]:
        return {
            "agent": "api-agent",
            "model": "test-model",
            "workspace": str(self.workspace),
            "tools": [],
            "warnings": [],
        }


def test_web_api_authenticates_and_streams_a_run(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}

        unauthorized = client.get("/api/v1/bootstrap")
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "unauthorized"

        missing = client.get("/api/v1/does-not-exist")
        assert missing.status_code == 401

        exchanged = client.get("/auth/exchange", params={"token": "launch-secret"})
        assert exchanged.status_code == 204

        bootstrap = client.get("/api/v1/bootstrap")
        assert bootstrap.status_code == 200
        assert bootstrap.json()["activeModel"] == "test"
        assert bootstrap.json()["approvalMode"] == "manual"

        missing = client.get("/api/v1/does-not-exist")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"

        headers = {"Origin": "http://testserver"}
        created = client.post("/api/v1/sessions", headers=headers)
        assert created.status_code == 201
        session_id = created.json()["sessionId"]

        started = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            headers=headers,
            json={"input": "hello", "clientRequestId": "web-request-1"},
        )
        assert started.status_code == 202
        run_id = started.json()["runId"]

        duplicate = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            headers=headers,
            json={"input": "hello", "clientRequestId": "web-request-1"},
        )
        assert duplicate.json()["runId"] == run_id

        stream = client.get(f"/api/v1/runs/{run_id}/events")
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: assistant.delta" in stream.text
        assert '"text":"hello from api"' in stream.text
        assert "event: run.completed" in stream.text

        snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
        assert snapshot["lastUserInput"] == "hello"
        assert snapshot["activeRunId"] is None


def test_web_api_rejects_mutation_from_wrong_origin(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        response = client.post(
            "/api/v1/sessions",
            headers={"Origin": "http://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_origin"


def test_web_api_rejects_authenticated_requests_with_unexpected_host(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        response = client.get(
            "/api/v1/bootstrap",
            headers={"Host": "attacker.invalid"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_host"


def test_web_api_replays_sse_after_last_event_id(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        run_id = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            headers=headers,
            json={"input": "hello", "clientRequestId": "replay-request"},
        ).json()["runId"]
        first = client.get(f"/api/v1/runs/{run_id}/events")
        replay = client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": "2"},
        )

    assert "id: 1" in first.text
    assert "id: 1" not in replay.text
    assert "id: 2" not in replay.text
    assert "event: run.completed" in replay.text


def test_web_api_serves_static_export_and_preserves_resume_query(tmp_path: Path) -> None:
    static = tmp_path / "web"
    asset = static / "_next" / "asset.js"
    asset.parent.mkdir(parents=True)
    (static / "index.html").write_text("<main>Lumen Web</main>", encoding="utf-8")
    asset.write_text("window.LUMEN = true", encoding="utf-8")
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", static_dir=static)

    with TestClient(app, base_url="http://testserver") as client:
        launched = client.get(
            "/",
            params={"token": "launch-secret", "session": "resume-me"},
            follow_redirects=False,
        )
        page = client.get(launched.headers["location"])
        script = client.get("/_next/asset.js")

    assert launched.status_code == 303
    assert launched.headers["location"] == "/?session=resume-me"
    assert page.text == "<main>Lumen Web</main>"
    assert script.text == "window.LUMEN = true"


def test_web_api_exposes_command_data_and_runs_mcp_prompt(tmp_path: Path) -> None:
    resources = ApiResources(tmp_path)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]

        sources = client.get(f"/api/v1/sessions/{session_id}/context/sources")
        prompts = client.get("/api/v1/mcp/prompts")
        hooks = client.get("/api/v1/hooks")
        started = client.post(
            f"/api/v1/sessions/{session_id}/controls",
            headers=headers,
            json={
                "type": "invoke_prompt",
                "name": "docs:summarize",
                "arguments": "/prompt docs:summarize topic='release notes'",
                "payload": {"arguments": {"topic": "release notes"}},
                "clientRequestId": "prompt-request-1",
            },
        )

        assert sources.json() == {
            "items": [
                {
                    "kind": "resource",
                    "reference": "docs:guide",
                    "revision": "rev-1",
                    "status": "available",
                }
            ]
        }
        assert prompts.json()["items"][0]["reference"] == "docs:summarize"
        assert hooks.json()["items"][0]["deny_count"] == 2
        assert started.status_code == 200
        run_id = started.json()["runId"]
        assert "event: run.completed" in client.get(f"/api/v1/runs/{run_id}/events").text
        snapshot = client.get(f"/api/v1/sessions/{session_id}").json()

    assert resources.rendered_prompts == [("docs:summarize", {"topic": "release notes"})]
    assert snapshot["lastUserInput"] == "/prompt docs:summarize topic='release notes'"
