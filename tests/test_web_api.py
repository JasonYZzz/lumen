from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.api import create_web_app
from lumen.application import WorkspaceHost
from lumen.attachments import AttachmentStore
from lumen.completion import CompletionGate
from lumen.config import LimitsConfig, LiveConfig, PermissionsConfig
from lumen.configuration import ConfigurationConflictError
from lumen.context import ArtifactStore
from lumen.live.manager import LiveSessionManager
from lumen.live.protocol import LiveCompletionControl, LiveMediaKind
from lumen.live.testing import FakeRealtimeTransport
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.ui.host_session import HostSessionAdapter


class ApiAgentOrchestrator:
    def __init__(self) -> None:
        self.event_sink = None
        self.messages: list[tuple[str, str]] = []

    def bind_root_run(self, _session_id: str, _run_id: str, *, approval_mode: str) -> None:
        return None

    def unbind_root_run(self, _run_id: str) -> None:
        return None

    def list(self, _session_id: str) -> list[object]:
        return []

    def usage_summary(self, _session_id: str, _root_run_id: str | None = None) -> dict[str, object]:
        return {"total": {}, "by_agent": {}}

    async def send_message(self, agent_id: str, message: str) -> str:
        self.messages.append((agent_id, message))
        return '{"id":"message-one"}'


class ApiConfigurationSnapshot:
    def __init__(self, revision: str, models: list[dict[str, object]]) -> None:
        self.revision = revision
        self.models = models

    def as_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "target_path": "/workspace/.lumen/agent.web.yaml",
            "editable": True,
            "edit_reason": None,
            "exclusive": False,
            "sources": [{"scope": "project", "path": "/workspace/.lumen/agent.yaml"}],
            "warnings": [],
            "default_model": str(self.models[0]["name"]),
            "models": self.models,
        }


class ApiConfiguration:
    def __init__(self) -> None:
        self.snapshot = ApiConfigurationSnapshot(
            "sha256:initial",
            [
                {
                    "name": "test",
                    "id": "test-model",
                    "api": None,
                    "base_url": None,
                    "api_key_env": None,
                    "settings": {},
                    "context": {},
                    "is_default": True,
                    "source": {"scope": "project", "path": "/workspace/.lumen/agent.yaml"},
                    "auth_kind": "none",
                    "auth_available": False,
                }
            ],
        )

    def inspect(self) -> ApiConfigurationSnapshot:
        return self.snapshot

    def upsert_model(
        self,
        *,
        expected_revision: str,
        name: str,
        definition: dict[str, object],
        set_default: bool,
    ) -> ApiConfigurationSnapshot:
        if expected_revision != self.snapshot.revision:
            raise ConfigurationConflictError("configuration changed")
        models = [item for item in self.snapshot.models if item["name"] != name]
        models.append(
            {
                "name": name,
                "id": definition["id"],
                "api": definition.get("api"),
                "base_url": definition.get("base_url"),
                "api_key_env": definition.get("api_key_env"),
                "settings": definition.get("settings", {}),
                "context": definition.get("context", {}),
                "is_default": set_default,
                "source": {"scope": "managed", "path": "/workspace/.lumen/agent.web.yaml"},
                "auth_kind": "environment" if definition.get("api_key_env") else "none",
                "auth_available": bool(definition.get("api_key_env")),
            }
        )
        self.snapshot = ApiConfigurationSnapshot("sha256:updated", models)
        return self.snapshot

    def remove_model(
        self,
        *,
        expected_revision: str,
        name: str,
    ) -> ApiConfigurationSnapshot:
        if expected_revision != self.snapshot.revision:
            raise ConfigurationConflictError("configuration changed")
        self.snapshot = ApiConfigurationSnapshot(
            "sha256:deleted",
            [item for item in self.snapshot.models if item["name"] != name],
        )
        return self.snapshot


class ApiResources:
    def __init__(self, root: Path) -> None:
        self.rendered_prompts: list[tuple[str, dict[str, str]]] = []

        async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
            yield "hello from api"

        self.workspace = root
        self.session_repository = SessionRepository(root / "sessions")
        self.artifact_store = ArtifactStore(root / "artifacts")
        self.agent_orchestrator = ApiAgentOrchestrator()
        self.configuration = ApiConfiguration()
        self.runtime = AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
            attachment_store=AttachmentStore(self.artifact_store),
        )
        self.config = SimpleNamespace(
            agent=SimpleNamespace(name="api-agent"),
            permissions=SimpleNamespace(default_mode="manual"),
        )
        self.tool_metadata: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []
        self.skills: list[object] = []
        self.live_manager: LiveSessionManager | None = None

    async def open(self) -> ApiResources:
        return self

    async def close(self) -> None:
        return None

    def active_model_config(self) -> SimpleNamespace:
        return SimpleNamespace(id="test-model", input_modalities=("text", "image"))

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

    def capabilities_report(self) -> dict[str, object]:
        return {
            "tools": [
                {
                    "name": "read_file",
                    "origin": "builtin",
                    "status": "loaded",
                    "approval": "allow",
                    "schema_digest": "sha256:test",
                }
            ],
            "skills": [],
            "mcp_servers": [],
            "agent_profiles": [],
        }

    def summary(self) -> dict[str, object]:
        return {
            "agent": "api-agent",
            "model": "test-model",
            "workspace": str(self.workspace),
            "tools": [],
            "warnings": [],
        }


def test_web_and_tui_adapters_expose_shared_operator_capabilities(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)
    paths: set[str] = set()
    for route in app.routes:
        path = cast(str | None, getattr(route, "path", None))
        if path is not None:
            paths.add(path)

    assert {
        "/api/v1/sessions/{session_id}/agents",
        "/api/v1/agents/{agent_id}/actions",
        "/api/v1/sessions/{session_id}/checkpoints",
        "/api/v1/sessions/{session_id}/archive",
        "/api/v1/sessions/{session_id}/fork",
        "/api/v1/runs/{run_id}/input/dequeue",
        "/api/v1/sessions/{session_id}/verification-waivers",
        "/api/v1/sessions/{session_id}/live",
        "/api/v1/live/{live_session_id}",
        "/api/v1/live/{live_session_id}/events",
        "/api/v1/live/{live_session_id}/interrupt",
        "/api/v1/capabilities",
        "/api/v1/configuration",
        "/api/v1/configuration/models/{model_name}",
    } <= paths
    for method in (
        "set_transcript_density",
        "dequeue_interactive",
        "list_child_runs",
        "send_agent_message",
        "continue_agent",
        "approve_agent_import",
        "reject_agent_import",
        "close_agent",
        "list_checkpoints",
        "fork_at_checkpoint",
        "work_state",
        "waive_effect",
    ):
        assert callable(getattr(HostSessionAdapter, method))


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
        assert bootstrap.json()["inputModalities"] == ["text", "image"]
        assert bootstrap.json()["approvalMode"] == "manual"
        assert bootstrap.json()["liveEnabled"] is False

        capabilities = client.get("/api/v1/capabilities")
        assert capabilities.status_code == 200
        assert capabilities.json()["tools"][0]["name"] == "read_file"

        configuration = client.get("/api/v1/configuration")
        assert configuration.status_code == 200
        assert configuration.json()["models"][0]["name"] == "test"

        headers = {"Origin": "http://testserver"}
        saved_model = client.put(
            "/api/v1/configuration/models/local",
            headers=headers,
            json={
                "expectedRevision": configuration.json()["revision"],
                "id": "openai:local",
                "api": "responses",
                "baseUrl": "http://127.0.0.1:11434/v1",
                "apiKeyEnv": "LOCAL_MODEL_KEY",
                "setDefault": True,
            },
        )
        assert saved_model.status_code == 200
        assert saved_model.json()["restartRequired"] is True
        assert saved_model.json()["models"][-1]["baseUrl"].endswith("/v1")

        stale_model = client.put(
            "/api/v1/configuration/models/stale",
            headers=headers,
            json={"expectedRevision": configuration.json()["revision"], "id": "test"},
        )
        assert stale_model.status_code == 409
        assert stale_model.json()["error"]["code"] == "configuration_conflict"

        missing = client.get("/api/v1/does-not-exist")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"

        created = client.post("/api/v1/sessions", headers=headers)
        assert created.status_code == 201
        session_id = created.json()["sessionId"]

        uploaded = client.post(
            "/api/v1/attachments?filename=diagram.png",
            headers={**headers, "content-type": "image/png"},
            content=b"\x89PNG\r\n\x1a\nweb-image",
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["artifactRef"].startswith("sha256:")

        started = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            headers=headers,
            json={
                "input": "hello",
                "clientRequestId": "web-request-1",
                "attachments": [uploaded.json()],
            },
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
        assert snapshot["workProducts"] == []
        assert snapshot["pendingEffects"] == []
        assert snapshot["recoverableEffects"] == []
        assert snapshot["agents"] == []
        assert snapshot["agentUsage"] == {"total": {}, "by_agent": {}}
        assert snapshot["liveSessions"] == []

        updated = client.patch(
            f"/api/v1/sessions/{session_id}/settings",
            headers=headers,
            json={"transcriptDensity": "verbose"},
        )
        assert updated.status_code == 200
        assert updated.json()["transcript_density"] == "verbose"

        checkpoints = client.get(f"/api/v1/sessions/{session_id}/checkpoints")
        assert checkpoints.status_code == 200
        assert checkpoints.json()["items"][0]["prompt"] == "hello"

        forked = client.post(
            f"/api/v1/sessions/{session_id}/fork",
            headers=headers,
            json={"throughTurn": 0},
        )
        assert forked.status_code == 201
        assert forked.json()["sessionId"] != session_id

        agents = client.get(f"/api/v1/sessions/{session_id}/agents")
        assert agents.status_code == 200
        assert agents.json() == {"items": []}

        sent = client.post(
            "/api/v1/agents/agent-one/actions",
            headers=headers,
            json={"action": "send_message", "message": "more context"},
        )
        assert sent.status_code == 200
        assert sent.json()["status"] == "queued"
        assert host.resources.agent_orchestrator.messages == [("agent-one", "more context")]

        renamed = client.patch(
            f"/api/v1/sessions/{session_id}",
            headers=headers,
            json={"title": "Managed conversation"},
        )
        assert renamed.status_code == 200
        assert renamed.json() == {"status": "renamed", "title": "Managed conversation"}

        archived = client.post(f"/api/v1/sessions/{session_id}/archive", headers=headers)
        assert archived.json() == {"status": "archived"}
        assert all(
            item["sessionId"] != session_id
            for item in client.get("/api/v1/sessions").json()["sessions"]
        )
        archived_items = client.get(
            "/api/v1/sessions", params={"include_archived": "true"}
        ).json()["sessions"]
        assert next(item for item in archived_items if item["sessionId"] == session_id) == {
            "sessionId": session_id,
            "createdAt": snapshot["createdAt"],
            "modelId": "test-model",
            "title": "Managed conversation",
            "archived": True,
        }

        restored = client.delete(f"/api/v1/sessions/{session_id}/archive", headers=headers)
        assert restored.json() == {"status": "restored"}
        deleted = client.delete(f"/api/v1/sessions/{session_id}", headers=headers)
        assert deleted.json() == {"status": "deleted"}
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404


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


def test_web_api_establishes_and_ends_a_live_session(tmp_path: Path) -> None:
    resources = ApiResources(tmp_path)
    transport = FakeRealtimeTransport()
    resources.live_manager = LiveSessionManager(
        config=LiveConfig(enabled=True, api_key="test"),
        router=transport.router(),
        repository=resources.session_repository,
        capability_gateway=CapabilityGateway(
            ToolRegistry(tmp_path),
            PermissionPolicy(PermissionsConfig()),
            default_timeout=2,
        ),
        completion_gate=CompletionGate(),
        plan_provider=lambda session_id: resources.session_repository.load(session_id).plan,
        context_documents=lambda _session_id: (),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        started = client.post(
            f"/api/v1/sessions/{session_id}/live",
            headers=headers,
            json={"clientRequestId": "live-request", "sdp": "v=0\r\noffer"},
        )
        live_id = started.json()["liveSessionId"]
        snapshot = client.get(f"/api/v1/live/{live_id}")
        interrupted = client.post(f"/api/v1/live/{live_id}/interrupt", headers=headers)
        ended = client.delete(f"/api/v1/live/{live_id}", headers=headers)
        events = client.get(f"/api/v1/live/{live_id}/events")

    assert started.status_code == 201
    assert started.json()["answerSdp"] == transport.answer_sdp
    assert snapshot.json()["connection"] == "active"
    assert interrupted.json()["status"] == "interrupted"
    assert ended.json()["status"] == "ended"
    assert "event: live.session.connected" in events.text
    assert "event: live.session.ended" in events.text


def test_web_api_streams_authenticated_pcm_to_a_host_controlled_provider(tmp_path: Path) -> None:
    resources = ApiResources(tmp_path)
    transport = FakeRealtimeTransport(
        media=LiveMediaKind.HOST_WEBSOCKET,
        completion_control=LiveCompletionControl.HOST_GATED_SYNTHESIS,
    )
    resources.live_manager = LiveSessionManager(
        config=LiveConfig(enabled=True, api_key="test"),
        router=transport.router(),
        repository=resources.session_repository,
        capability_gateway=CapabilityGateway(
            ToolRegistry(tmp_path),
            PermissionPolicy(PermissionsConfig()),
            default_timeout=2,
        ),
        completion_gate=CompletionGate(),
        plan_provider=lambda session_id: resources.session_repository.load(session_id).plan,
        context_documents=lambda _session_id: (),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        started = client.post(
            f"/api/v1/sessions/{session_id}/live",
            headers=headers,
            json={"clientRequestId": "live-pcm"},
        )
        live_id = started.json()["liveSessionId"]
        assert started.json()["media"] == {
            "kind": "host_websocket",
            "media_path": f"/api/v1/live/{live_id}/media",
            "input_sample_rate": 16_000,
            "output_sample_rate": 24_000,
        }
        with client.websocket_connect(
            f"/api/v1/live/{live_id}/media",
            headers={"Origin": "http://testserver"},
        ) as media:
            media.send_bytes(b"\x01\x02\x03\x04")
            for _ in range(50):
                if transport.connections[0].audio:
                    break
                time.sleep(0.01)

    assert transport.connections[0].audio == [b"\x01\x02\x03\x04"]


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
