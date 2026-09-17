from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.api import create_web_app
from lumen.application import WorkspaceHost
from lumen.attachments import AttachmentStore
from lumen.completion import CompletionGate
from lumen.config import AgentSection, LimitsConfig, LiveConfig, ModelSettingsConfig, PermissionsConfig
from lumen.configuration import ConfigurationConflictError
from lumen.context import ArtifactStore
from lumen.live.manager import LiveSessionManager
from lumen.live.protocol import LiveCompletionControl, LiveMediaKind
from lumen.live.testing import FakeRealtimeTransport
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind
from lumen.ui.host_session import HostSessionAdapter
from lumen.work_products import TaskWorkspace


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
    def __init__(
        self,
        revision: str,
        models: list[dict[str, object]],
        mcp_servers: list[dict[str, object]] | None = None,
        default_model: str | None = None,
    ) -> None:
        self.revision = revision
        self.models = models
        self.default_model = default_model or str(models[0]["name"])
        self.mcp_servers = mcp_servers or [
            {"name": "exa", "enabled": True, "source": {"scope": "project", "path": "agent.yaml"}}
        ]

    def as_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "target_path": "/workspace/.lumen/agent.web.yaml",
            "editable": True,
            "edit_reason": None,
            "exclusive": False,
            "sources": [{"scope": "project", "path": "/workspace/.lumen/agent.yaml"}],
            "warnings": [],
            "default_model": self.default_model,
            "models": self.models,
            "mcp_servers": self.mcp_servers,
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

    def resolved_agent(self) -> AgentSection:
        return AgentSection(
            models={
                str(item["name"]): ModelSettingsConfig.model_validate(
                    {
                        "id": item["id"],
                        "api": item.get("api"),
                        "base_url": item.get("base_url"),
                        "api_key_env": item.get("api_key_env"),
                        "settings": item.get("settings", {}),
                        "context": item.get("context", {}),
                        "input_modalities": item.get("input_modalities", ("text",)),
                    }
                )
                for item in self.snapshot.models
            },
            default_model=self.snapshot.default_model,
        )

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
        previous_default = self.snapshot.default_model
        default_model = name if set_default else previous_default
        models = [
            {**item, "is_default": item["name"] == default_model}
            for item in self.snapshot.models
            if item["name"] != name
        ]
        models.append(
            {
                "name": name,
                "id": definition["id"],
                "api": definition.get("api"),
                "base_url": definition.get("base_url"),
                "api_key_env": definition.get("api_key_env"),
                "settings": definition.get("settings", {}),
                "context": definition.get("context", {}),
                "input_modalities": definition.get("input_modalities", ("text",)),
                "is_default": name == default_model,
                "source": {"scope": "managed", "path": "/workspace/.lumen/agent.web.yaml"},
                "auth_kind": "environment" if definition.get("api_key_env") else "none",
                "auth_available": bool(definition.get("api_key_env")),
            }
        )
        self.snapshot = ApiConfigurationSnapshot(
            "sha256:updated",
            models,
            default_model=default_model,
        )
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
            default_model=(
                next(str(item["name"]) for item in self.snapshot.models if item["name"] != name)
                if self.snapshot.default_model == name
                else self.snapshot.default_model
            ),
        )
        return self.snapshot

    def set_mcp_server_enabled(
        self,
        *,
        expected_revision: str,
        name: str,
        enabled: bool,
    ) -> ApiConfigurationSnapshot:
        if expected_revision != self.snapshot.revision:
            raise ConfigurationConflictError("configuration changed")
        self.snapshot = ApiConfigurationSnapshot(
            "sha256:mcp-updated",
            self.snapshot.models,
            [
                {**server, "enabled": enabled} if server["name"] == name else server
                for server in self.snapshot.mcp_servers
            ],
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
        self.task_workspace: TaskWorkspace | None = None
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
        self._active_model = "test"
        self._model_registry = {
            "test": ModelSettingsConfig(id="test-model", input_modalities=("text", "image"))
        }

    async def open(self) -> ApiResources:
        return self

    async def close(self) -> None:
        return None

    def active_model_config(self) -> Any:
        return self._model_registry[self._active_model]

    def active_model_name(self) -> str:
        return self._active_model

    def available_models(self) -> list[str]:
        return sorted(self._model_registry)

    async def apply_model_configuration(
        self,
        agent: AgentSection,
        *,
        active_model_name: str | None = None,
    ) -> None:
        self._model_registry = agent.model_registry()
        self._active_model = active_model_name or agent.default_model_name()

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

    def instructions_report(self) -> dict[str, object]:
        return {
            "mode": "preset",
            "preset": "lumen",
            "version": "test-v1",
            "digest": "sha256:test",
            "characters": 42,
            "runtime_context_characters": 12,
            "active_model": "test",
            "model_id": "test-model",
            "sources": [
                {
                    "origin": "builtin:lumen",
                    "role": "system",
                    "revision": "sha256:source",
                    "characters": 42,
                }
            ],
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


def test_document_content_uses_authenticated_host_read_and_safe_download_headers(tmp_path: Path) -> None:
    (tmp_path / "report.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/api/v1/files/content", params={"path": "report.html"}).status_code == 401
        client.get("/auth/exchange", params={"token": "launch-secret"})
        response = client.get("/api/v1/files/content", params={"path": "report.html"})
        if sys.platform == "win32":
            assert response.status_code == 400
            # Host intentionally redacts filesystem failure details.
            assert response.json()["error"]["code"] == "invalid_state"
            assert "Document unavailable" in response.text
            return
        assert response.status_code == 200
        assert response.content == b"<script>alert(1)</script>"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
        assert response.headers["cache-control"] == "no-store"
        for path in ("../report.html", ".env", "missing.md"):
            assert client.get("/api/v1/files/content", params={"path": path}).status_code == 400


def test_session_modes_are_persisted_separately_from_workspace_defaults(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)
    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        updated = client.patch(
            f"/api/v1/sessions/{session_id}/settings",
            headers=headers,
            json={"approvalMode": "auto", "collaborationMode": "plan"},
        )
        assert updated.status_code == 200
        assert updated.json()["approval_mode"] == "auto"
        assert updated.json()["collaboration_mode"] == "plan"
        defaults = client.get("/api/v1/bootstrap").json()
        assert defaults["approvalMode"] == "manual"
        assert defaults["collaborationMode"] == "default"
        current = client.get(f"/api/v1/sessions/{session_id}").json()
        assert current["approvalMode"] == "auto"
        assert current["collaborationMode"] == "plan"
        other_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        other = client.get(f"/api/v1/sessions/{other_id}").json()
        assert other["approvalMode"] == "manual"
        assert other["collaborationMode"] == "default"


def test_reasoning_api_validates_persists_and_reports_effective_parameters(tmp_path: Path) -> None:
    resources = ApiResources(tmp_path)
    resources.active_model_config = lambda: ModelSettingsConfig(  # type: ignore[assignment]
        id="openai:gpt-6-astra", reasoning_effort="medium",  # type: ignore[arg-type]
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)
    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        assert client.get("/api/v1/bootstrap").json()["reasoning"]["effective"] == "medium"
        endpoint = f"/api/v1/sessions/{session_id}/settings"
        response = client.patch(endpoint, headers=headers, json={"reasoningEffort": "low"})
        assert response.status_code == 200
        selected = client.get(f"/api/v1/sessions/{session_id}").json()["reasoning"]
        assert selected["effective"] == "low"
        assert selected["parameters"]["openai_reasoning_effort"] == "low"
        assert client.patch(endpoint, headers=headers, json={"reasoningEffort": "bogus"}).status_code == 422
        assert client.patch(endpoint, headers=headers, json={"reasoningEffort": "off"}).status_code == 400
        assert client.get(f"/api/v1/sessions/{session_id}").json()["reasoning"]["effective"] == "low"


def test_reasoning_preview_uses_draft_model_and_does_not_change_configuration(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)
    with TestClient(app, base_url="http://testserver") as client:
        endpoint = "/api/v1/configuration/reasoning"
        body = {"expectedRevision": "sha256:preview", "id": "openai:deepseek-flash",
                "api": "responses", "baseUrl": "https://api.deepseek.com"}
        assert client.post(endpoint, json=body).status_code == 401
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        before = client.get("/api/v1/configuration").json()
        response = client.post(endpoint, headers=headers, json=body)
        assert response.status_code == 200, response.json()
        capability = response.json()["reasoning"]
        assert capability["supported_levels"] == [
            "provider_default", "off", "minimal", "low", "medium", "high", "xhigh", "max",
        ]
        assert capability["level_map"]["medium"] == "high"
        assert capability["catalog_revision"]
        assert capability["provider"] == "deepseek"
        assert capability["capability_documents"][0].startswith("https://api-docs.deepseek.com/")
        assert capability["requested"] is None
        body.update(id="openai:k3", baseUrl="https://api.kimi.com/coding/v1")
        capability = client.post(endpoint, headers=headers, json=body).json()["reasoning"]
        assert "off" not in capability["supported_levels"]
        assert capability["level_map"]["xhigh"] == "max"
        invalid = client.post(endpoint, headers=headers, json={**body, "reasoningLevels": ["off"]})
        assert invalid.status_code == 400
        body.update(id="anthropic:qwen3.8-max", baseUrl="https://test.maas.aliyuncs.com/apps/anthropic")
        capability = client.post(endpoint, headers=headers, json=body).json()["reasoning"]
        assert capability["level_map"]["high"] == "xhigh"
        assert client.get("/api/v1/configuration").json() == before
        profile_body = {"expectedRevision": "sha256:preview", "id": "openai:gpt-5.6",
                        "api": "responses", "baseUrl": "https://proxy.example/v1",
                        "reasoningProfile": "openai-gpt56-sol"}
        capability = client.post(endpoint, headers=headers, json=profile_body).json()["reasoning"]
        assert capability["capability_source"] == "deployment_profile:openai-gpt56-sol"
        assert "max" in capability["supported_levels"]
        invalid = client.post(endpoint, headers=headers, json={**profile_body, "id": "openai:unknown"})
        assert invalid.status_code == 400


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
        assert configuration.json()["mcpServers"][0]["enabled"] is True

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
                "inputModalities": ["text", "image"],
                "setDefault": True,
            },
        )
        assert saved_model.status_code == 200
        assert saved_model.json()["restartRequired"] is False
        assert saved_model.json()["activeModel"] == "local"
        assert saved_model.json()["models"][-1]["baseUrl"].endswith("/v1")

        mcp_toggled = client.patch(
            "/api/v1/configuration/mcp/exa",
            headers=headers,
            json={"expectedRevision": saved_model.json()["revision"], "enabled": False},
        )
        assert mcp_toggled.status_code == 200
        assert mcp_toggled.json()["mcpServers"][0]["enabled"] is False

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

        preview = client.post("/api/v1/attachments/content", headers=headers, json=uploaded.json())
        assert preview.status_code == 200
        assert preview.content == b"\x89PNG\r\n\x1a\nweb-image"
        assert preview.headers["content-type"] == "image/png"
        assert preview.headers["x-content-type-options"] == "nosniff"
        for invalid in (
            {**uploaded.json(), "mediaType": "text/html"},
            {**uploaded.json(), "byteSize": 1},
            {**uploaded.json(), "artifactRef": "sha256:" + "0" * 64},
        ):
            rejected = client.post("/api/v1/attachments/content", headers=headers, json=invalid)
            assert rejected.status_code == 400

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
        user_row = next(item for item in snapshot["timeline"] if item["kind"] == "user")
        assert user_row["elapsed_seconds"] >= 0
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

        edited = client.post(
            f"/api/v1/sessions/{session_id}/fork", headers=headers,
            json={"throughTurn": 0, "includeTurn": False, "clientRequestId": "edit-first"},
        )
        assert edited.status_code == 201
        retried = client.post(
            f"/api/v1/sessions/{session_id}/fork", headers=headers,
            json={"throughTurn": 0, "includeTurn": False, "clientRequestId": "edit-first"},
        )
        assert retried.json() == edited.json()
        empty_branch = client.get(f"/api/v1/sessions/{edited.json()['sessionId']}")
        assert empty_branch.json()["timeline"] == []
        users = [item for item in snapshot["timeline"] if item["kind"] == "user"]
        assert users[0]["turn_index"] == 0
        assert users[0]["interaction_id"]
        assert users[0]["attachments"][0]["filename"] == "diagram.png"
        invalid = client.post(
            f"/api/v1/sessions/{session_id}/fork", headers=headers,
            json={"throughTurn": 0, "includeTurn": "false"},
        )
        assert invalid.status_code == 422

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
                "modelId": "openai:local",
            "title": "Managed conversation",
            "archived": True,
            "titlePending": False,
        }

        restored = client.delete(f"/api/v1/sessions/{session_id}/archive", headers=headers)
        assert restored.json() == {"status": "restored"}
        deleted = client.delete(f"/api/v1/sessions/{session_id}", headers=headers)
        assert deleted.json() == {"status": "deleted"}
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404


def test_web_api_regenerates_in_place_without_creating_a_second_session(tmp_path: Path) -> None:
    host = WorkspaceHost(ApiResources(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)

    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        for index, prompt in enumerate(("prefix", "same prompt", "stale follow-up")):
            started = client.post(
                f"/api/v1/sessions/{session_id}/runs",
                headers=headers,
                json={"input": prompt, "clientRequestId": f"old-{index}"},
            )
            assert started.status_code == 202
            assert client.get(f"/api/v1/runs/{started.json()['runId']}/events").status_code == 200
        assert client.patch(
            f"/api/v1/sessions/{session_id}",
            headers=headers,
            json={"title": "Stable title"},
        ).status_code == 200

        regenerated = client.post(
            f"/api/v1/sessions/{session_id}/runs",
            headers=headers,
            json={
                "input": "same prompt",
                "clientRequestId": "same-text-regeneration",
                "regenerateFromTurn": 1,
            },
        )
        assert regenerated.status_code == 202
        assert regenerated.json()["sessionId"] == session_id
        assert client.get(f"/api/v1/runs/{regenerated.json()['runId']}/events").status_code == 200

        snapshot = client.get(f"/api/v1/sessions/{session_id}").json()
        assert [item["text"] for item in snapshot["timeline"] if item["kind"] == "user"] == [
            "prefix",
            "same prompt",
        ]
        sessions = client.get("/api/v1/sessions").json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["title"] == "Stable title"


def test_web_delete_failed_research_preserves_effect_journal_and_is_idempotent(tmp_path: Path) -> None:
    resources = ApiResources(tmp_path)
    work = TaskWorkspace(tmp_path, resources.artifact_store, resources.session_repository)
    resources.task_workspace = work
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="launch-secret", api_only=True)
    with TestClient(app, base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "launch-secret"})
        headers = {"Origin": "http://testserver"}
        session_id = client.post("/api/v1/sessions", headers=headers).json()["sessionId"]
        work.bind_session(session_id)
        for _ in range(6):
            work.record_tool_effect(tool_name="exa_web_search_exa", effect_kind=EffectKind.UNKNOWN,
                                    success=True, summary="historical search")
        before = resources.session_repository.load(session_id)
        assert len(client.get(f"/api/v1/sessions/{session_id}").json()["pendingEffects"]) == 6
        for _ in range(2):
            deleted = client.delete(f"/api/v1/sessions/{session_id}", headers=headers)
            assert deleted.status_code == 200
            assert deleted.json() == {"status": "deleted"}
        assert client.get(f"/api/v1/sessions/{session_id}").status_code == 404
        after = resources.session_repository.load(session_id)
        assert after.work_state == before.work_state
        assert after.catalog.deleted_at is not None


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
        assert client.delete(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 400
        assert client.post(f"/api/v1/sessions/{session_id}/archive", headers=headers).status_code == 400
        interrupted = client.post(f"/api/v1/live/{live_id}/interrupt", headers=headers)
        ended = client.delete(f"/api/v1/live/{live_id}", headers=headers)
        events = client.get(f"/api/v1/live/{live_id}/events")
        assert client.delete(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 200

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
        instructions = client.get("/api/v1/instructions")
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
        assert instructions.json()["mode"] == "preset"
        assert instructions.json()["sources"][0]["origin"] == "builtin:lumen"
        assert hooks.json()["items"][0]["deny_count"] == 2
        assert started.status_code == 200
        run_id = started.json()["runId"]
        assert "event: run.completed" in client.get(f"/api/v1/runs/{run_id}/events").text
        snapshot = client.get(f"/api/v1/sessions/{session_id}").json()

    assert resources.rendered_prompts == [("docs:summarize", {"topic": "release notes"})]
    assert snapshot["lastUserInput"] == "/prompt docs:summarize topic='release notes'"
