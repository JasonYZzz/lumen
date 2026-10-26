from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from test_automation import resources_for, wait_settled

from lumen.api import create_web_app
from lumen.application import (
    ApplyAutomationCommand,
    ContinueAssistantDiscussion,
    DeleteSession,
    EditAssistant,
    GetGoalDetail,
    InvalidStateError,
    PublishAssistantResource,
    ReadAssistantResource,
    SessionNotFoundError,
    SetApprovalMode,
    StartRun,
    WorkspaceHost,
)
from lumen.automation.models import (
    AssistantEdit,
    AutomationCommand,
    AutomationInput,
    ContextRef,
    Definition,
    Goal,
    GoalInput,
    Occurrence,
    PublishInput,
    Resource,
    Result,
)
from lumen.automation.store import AutomationStore
from lumen.config import LimitsConfig, PermissionsConfig, PluginConfig, WebSearchConfig, load_config
from lumen.resources import ResourceManager
from lumen.runtime import AgentRuntime
from lumen.tools.gateway import CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import Risk, ToolSpec
from lumen.tools.web import build_web_search_spec
from lumen.work_products import TaskWorkspace


def test_enable_admits_previously_queued_run_without_revoking_validation(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "state.db")
    store.open()
    try:
        now = datetime.now(UTC)
        identity = store.command(
            AutomationCommand(
                action="create",
                request_id="new",
                spec=AutomationInput(name="Review", intent="Review", paths=("README.md",)),
            ),
            "fp",
            now,
        )
        trial_id = store.command(
            AutomationCommand(action="trial", request_id="trial", id=identity, expected_revision=1), "fp", now
        )
        trial = store.get("occurrence", trial_id, Occurrence)
        store.settle(trial.model_copy(update={"result": Result.COMPLETE, "fingerprint": "fp"}))
        queued_id = store.command(
            AutomationCommand(action="run", request_id="run", id=identity, expected_revision=1), "fp", now
        )
        stale = store.get("occurrence", queued_id, Occurrence)
        store.command(
            AutomationCommand(action="enable", request_id="enable", id=identity, expected_revision=1),
            "fp",
            now,
        )
        admitted = store.begin(stale, "fp", now)
        assert admitted.definition_revision == 2
        store.settle(admitted.model_copy(update={"result": Result.COMPLETE}))
        assert store.get("definition", identity, Definition).enabled
        assert store.get("definition", identity, Definition).validation == "fp"
        store.settle(trial.model_copy(update={"result": Result.NEEDS_USER_ACTION}))
        assert store.get("definition", identity, Definition).enabled
    finally:
        store.close()


def test_v1_migration_keeps_id_history_and_curates_without_deleting(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = AutomationStore(path)
    store.open()
    now = datetime.now(UTC)
    with store.transaction() as db:
        for identity, kind in (("report", "report"), ("temporary", "work_product")):
            db.execute(
                "INSERT INTO objects VALUES('resource',?,?)",
                (
                    identity,
                    json.dumps(
                        {
                            "id": identity,
                            "title": identity,
                            "kind": kind,
                            "artifact_ref": "sha256:" + "a" * 64,
                            "created_at": now.isoformat(),
                        }
                    ),
                ),
            )
        db.execute(
            "INSERT INTO objects VALUES('goal','old-goal',?)",
            (
                json.dumps(
                    {
                        "id": "old-goal",
                        "revision": 1,
                        "title": "Keep me",
                        "milestones": [],
                        "automation_ids": [],
                    }
                ),
            ),
        )
        for name in (
            "resource_versions",
            "goal_resources",
            "collection_resources",
            "resource_activity",
            "assistant_runs",
        ):
            db.execute(f"DROP TABLE {name}")
        db.execute("PRAGMA user_version=1")
    store.close()
    store.open()
    try:
        assert store.get("goal", "old-goal", Goal).primary_session_id is None
        assert [r["id"] for r in store.page("resource", None, "", "all", 20)["items"]] == ["report"]
        assert store.page("resource", None, "", "materials", 20)["items"][0]["id"] == "temporary"
        assert len(list(tmp_path.glob("*.bak"))) == 1
        with store.transaction() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        store.close()


async def test_goal_discussion_publication_versions_and_library_share_identity(tmp_path: Path) -> None:
    resources = resources_for(tmp_path, strict_background=False)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        await host.dispatch(
            EditAssistant(
                AssistantEdit(
                    action="goal",
                    id="goal",
                    goal=GoalInput(title="Understand a topic", intent="Explain it using my notes"),
                )
            )
        )
        first = await host.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        second = await host.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        assert first.data == second.data
        identity = cast(str, first.data["session_id"])
        started = await host.dispatch(StartRun(identity, "Produce a report", "one", publish_result=True))
        events = [event async for event in host.subscribe(started.run_id)]
        assert events[-1].type == "run.completed"
        detail = (await host.dispatch(GetGoalDetail("goal"))).data
        assert detail["resources"] == []  # Ordinary answer text is not an automatically saved file.
        await host.dispatch(PublishAssistantResource(PublishInput(
            session_id=identity, interaction_id=started.run_id, title="Explicitly saved note", goal_id="goal",
        )))
        detail = (await host.dispatch(GetGoalDetail("goal"))).data
        result_id = detail["resources"][0]["id"]
        read = (await host.dispatch(ReadAssistantResource(result_id))).data
        assert read["resource"]["session_id"] == identity
        assert read["goal_ids"] == ["goal"]
        assert result_id == f"answer:{started.run_id}"
        journal = resources.session_repository.load(identity)
        assert "Understand a topic" in str(journal.turns[0].messages)
        await host.dispatch(
            EditAssistant(
                AssistantEdit(
                    action="edit_resource",
                    id=result_id,
                    title="Updated report",
                    content="Reviewed version",
                    expected_revision=1,
                )
            )
        )
        assert (await host.dispatch(ReadAssistantResource(result_id, revision=1))).data["content"] == read[
            "content"
        ]
        assert (await host.dispatch(ReadAssistantResource(result_id))).data["content"] == "Reviewed version"
        discussion = await host.dispatch(
            ContinueAssistantDiscussion(ContextRef(kind="resource", id=result_id, revision=2))
        )
        assert discussion.data["context_refs"][0]["revision"] == 2
        await host.dispatch(
            EditAssistant(AssistantEdit(action="collection", id="collection", title="Research"))
        )
        await host.dispatch(
            EditAssistant(AssistantEdit(action="collect", id=result_id, collection_id="collection"))
        )
        page = host.automations.store.page("resource", None, "Reviewed", "collection:collection", 20)
        assert page["items"][0]["id"] == result_id
        await host.dispatch(EditAssistant(AssistantEdit(action="visibility", id=result_id, value=False)))
        assert host.automations.store.page("resource", None, "", "all", 20)["items"] == []
        assert (await host.dispatch(ReadAssistantResource(result_id))).data["content"] == "Reviewed version"
        with pytest.raises(InvalidStateError):
            await host.dispatch(
                PublishAssistantResource(
                    PublishInput(
                        session_id=identity, interaction_id="nonexistent", title="Do not invent a result"
                    )
                )
            )
        await host.dispatch(EditAssistant(AssistantEdit(action="note", id="note", title="Empty page")))
        assert (await host.dispatch(ReadAssistantResource("note"))).data["content"] == ""
    finally:
        await host.close()


async def test_reserved_discussion_recovers_after_restart_but_deleted_discussion_is_not_recreated(
    tmp_path: Path,
) -> None:
    first = WorkspaceHost(resources_for(tmp_path, strict_background=False))  # type: ignore[arg-type]
    await first.open()
    await first.dispatch(
        EditAssistant(AssistantEdit(action="goal", id="goal", goal=GoalInput(title="Recover")))
    )
    identity = first.automations.store.discussion(ContextRef(kind="goal", id="goal"))
    await first.close()
    restarted = WorkspaceHost(resources_for(tmp_path, strict_background=False))  # type: ignore[arg-type]
    await restarted.open()
    try:
        assert restarted.automations.status == "running"
        discussion = await restarted.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        assert discussion.data["session_id"] == identity
        path = restarted.resources.session_repository.load(identity).metadata.path
        await restarted.dispatch(DeleteSession(identity))
        before = path.read_bytes()
        with pytest.raises(InvalidStateError, match="deleted"):
            await restarted.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        assert path.read_bytes() == before
    finally:
        await restarted.close()


async def test_explicit_rejected_publication_keeps_completed_answer_and_does_not_stop_the_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = WorkspaceHost(resources_for(tmp_path, strict_background=False))  # type: ignore[arg-type]
    await host.open()
    try:
        await host.dispatch(
            EditAssistant(AssistantEdit(action="goal", id="goal", goal=GoalInput(title="Save")))
        )
        discussion = await host.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        identity = cast(str, discussion.data["session_id"])

        def reject(_resource: Resource, *, goal_id: str | None = None, text: str = "") -> Resource:
            raise ValueError("Injected content rejection")

        monkeypatch.setattr(host.automations.store, "publish", reject)
        started = await host.dispatch(StartRun(identity, "Produce a report", "one", publish_result=True))
        assert [event.type async for event in host.subscribe(started.run_id)][-1] == "run.completed"
        assert host.automations.status == "running"
        with pytest.raises(InvalidStateError):
            await host.dispatch(PublishAssistantResource(PublishInput(
                session_id=identity, interaction_id=started.run_id, title="Save", goal_id="goal",
            )))
        assert host.automations.store.feed() == ()
        assert host.resources.session_repository.load(identity).turns[0].status == "completed"
        next_run = await host.dispatch(StartRun(identity, "Continue", "two"))
        assert [event.type async for event in host.subscribe(next_run.run_id)][-1] == "run.completed"
    finally:
        await host.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Descriptor-safe file collection requires POSIX")
@pytest.mark.parametrize("tracked", [False, True])
async def test_generated_files_are_collected_linked_and_versioned_without_saving_chat(
    tmp_path: Path, tracked: bool,
) -> None:
    resources = resources_for(tmp_path, strict_background=False)
    if tracked:
        resources.task_workspace = TaskWorkspace(
            tmp_path, resources.artifact_store, resources.session_repository,
        )
    (tmp_path / "old.md").write_text("Existing source")
    os.utime(tmp_path / "old.md", (1, 1))
    (tmp_path / "escape.md").symlink_to(tmp_path / "old.md")
    step = 0

    def create_outputs() -> str:
        text = f"# Actual document {step}"
        if resources.task_workspace is not None:
            resources.task_workspace.perform_text_mutation(
                "report.md", operation="write_file", selector="whole", change=text,
                action=lambda: (tmp_path / "report.md").write_text(text),
            )
        else:
            (tmp_path / "report.md").write_text(text)
        (tmp_path / "site.html").write_text(f"<h1>Actual website {step}</h1>")
        (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
        return "Created files"

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal step
        step += 1
        if step % 2:
            yield {0: DeltaToolCall("create_outputs", "{}", tool_call_id=f"write-{step}")}
        else:
            yield (
                "A conversational explanation, not the document.\n\n"
                "[Report](report.md) [Website](site.html) ![Image](image.png) "
                "[Old input](old.md) [Missing](missing.pdf) [Escape](escape.md) [Private](.env)"
            )

    resources.runtime = AgentRuntime(model=FunctionModel(stream_function=stream),
        tools=[Tool(create_outputs, requires_approval=True)], toolsets=[], instructions="Produce files",
        limits=LimitsConfig(), tool_metadata={"create_outputs": {"origin": "plugin:test", "risk": "write"}})
    host = await WorkspaceHost(resources).open()  # type: ignore[arg-type]
    try:
        await host.dispatch(EditAssistant(AssistantEdit(
            action="goal", id="goal", goal=GoalInput(title="Files"),
        )))
        discussion = await host.dispatch(ContinueAssistantDiscussion(ContextRef(kind="goal", id="goal")))
        session_id = cast(str, discussion.data["session_id"])
        await host.dispatch(SetApprovalMode(session_id, "auto"))
        first_body: bytes | None = None
        first_id = ""
        for revision in (1, 2):
            started = await host.dispatch(StartRun(session_id, "Create files", f"run-{revision}"))
            events = [event async for event in host.subscribe(started.run_id)]
            assert events[-1].type == "run.completed"
            items = host.automations.store.page("resource", None, "", "all", 50)["items"]
            assert {item["file_path"] for item in items} == {"report.md", "site.html", "image.png"}
            assert {item["file_type"] for item in items} == {"document", "site", "image"}
            assert all(item["interaction_id"] == started.run_id for item in items)
            assert len((await host.dispatch(GetGoalDetail("goal"))).data["resources"]) == 3
            feed = host.automations.store.feed()
            assert len(feed) == revision * 2 + 1  # The unchanged image must not produce another update.
            assert {item.resource_id for item in feed} == {item["id"] for item in items}
            assert all(item.goal_id == "goal" and item.session_id == session_id for item in feed)
            report = next(item for item in items if item["file_path"] == "report.md")
            assert report["revision"] == revision
            identity, body = await host.read_resource_document(report["id"])
            assert body.startswith(b"# Actual document")
            assert b"conversational" not in body
            if revision == 1:
                first_body, first_id = body, identity.id
            else:
                assert body != first_body
                assert (await host.read_resource_document(first_id, 1))[1] == first_body
        (tmp_path / "report.md").unlink()
        assert (await host.read_resource_document(first_id, 1))[1] == first_body
        app = create_web_app(host, launch_token="file-review", api_only=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            denied = await client.get(f"/api/v1/assistant/resources/{first_id}/content?revision=1")
            assert denied.status_code == 401
            assert (await client.get("/auth/exchange?token=file-review")).status_code == 204
            response = await client.get(f"/api/v1/assistant/resources/{first_id}/content?revision=1")
            assert response.status_code == 200 and response.content == first_body
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
            page = await client.get(f"/api/v1/assistant/resources?session_id={session_id}&category=image")
            assert [item["file_path"] for item in page.json()["items"]] == ["image.png"]
        await host.dispatch(DeleteSession(session_id))
        with pytest.raises((InvalidStateError, SessionNotFoundError)):
            await host.read_resource_document(first_id)
    finally:
        await host.close()


@pytest.mark.parametrize("allowed", [True, False])
async def test_research_uses_configured_gateway_and_actual_http_query_without_files(
    tmp_path: Path,
    allowed: bool,
) -> None:
    queries: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        queries.append(body["query"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Release",
                        "url": "https://example.com/news",
                        "content": "A verifiable search snippet",
                        "published_date": "2026-10-21",
                    }
                ]
            },
        )

    resources = resources_for(tmp_path)
    registry = ToolRegistry(tmp_path)
    registry.add(
        build_web_search_spec(
            WebSearchConfig(provider="tavily", api_key_env="TEST_SEARCH_TOKEN"),
            transport=httpx.MockTransport(respond),
            environ={"TEST_SEARCH_TOKEN": "fixture-token"},
        ),
        origin="builtin:web",
    )
    resources.capability_gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig(always_allow=["web_search"] if allowed else [])),
        default_timeout=10,
    )
    prompts: list[str] = []

    async def answer(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        assert not info.function_tools
        prompts.append(str(messages))
        yield "A source-backed review: https://example.com/news"

    assert resources.runtime is not None
    from lumen.runtime import AgentRuntime

    resources.runtime = AgentRuntime(
        model=FunctionModel(stream_function=answer),
        tools=[],
        toolsets=[],
        instructions="Summarize",
        limits=resources.runtime.limits,
        tool_metadata={},
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    action="create",
                    request_id="new",
                    spec=AutomationInput(name="News", intent="AI news today", profile="research_readonly"),
                )
            )
        )
        trial = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    action="trial", request_id="trial", id=created.data["id"], expected_revision=1
                )
            )
        )
        state = await wait_settled(host, trial.data["id"])
        occurrence = state.occurrences[0]
        if allowed:
            assert occurrence.result is Result.COMPLETE
            assert queries == ["AI news today"]
            assert "https://example.com/news" in prompts[0]
            assert occurrence.receipts[0].coverage == "search_snippet"
            assert occurrence.receipts[0].artifact_ref
        else:
            assert occurrence.result in {Result.FAILED, Result.NEEDS_USER_ACTION}
            assert queries == prompts == []
        assert not state.definitions[0].enabled
    finally:
        await host.close()


@pytest.mark.parametrize(
    "name",
    ["inspect_personal_assistant", "manage_automation", "read_assistant_resource", "manage_personal_goal"],
)
async def test_host_tool_collisions_fail_during_setup_with_a_clear_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    from lumen.tools.registry import DuplicateToolError

    def plugin_tool() -> str:
        return "unused"

    def plugin_specs(_config: PluginConfig, *, search_path: str | Path | None = None) -> list[ToolSpec]:
        return [ToolSpec(plugin_tool, name=name, risk=Risk.READ)]

    monkeypatch.setattr("lumen.resources.load_plugin_specs", plugin_specs)
    path = tmp_path / "agent.yaml"
    path.write_text(
        "version: 2\nagent: {model: {id: test}}\ntools:\n  builtins: []\n  plugins: [{module: fixture}]\n"
    )
    with pytest.raises(DuplicateToolError, match="reserved runtime names"):
        async with ResourceManager(load_config(path), workspace=tmp_path):
            pytest.fail("A colliding plugin must be rejected before accepting conversations")
