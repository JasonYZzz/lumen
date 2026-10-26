from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_automation import resources_for

from lumen.api import create_web_app
from lumen.application import (
    ConfigurationConflictHostError,
    CreateSession,
    EditAssistant,
    InvalidStateError,
    ReadAssistantResource,
    StartRun,
    WorkspaceHost,
)
from lumen.automation.models import (
    AssistantEdit,
    AutomationCommand,
    AutomationInput,
    ContextRef,
    FeedItem,
    Goal,
    GoalCommand,
    GoalExecutionStatus,
    GoalInput,
    Milestone,
    Occurrence,
    Resource,
    Result,
)
from lumen.automation.store import AssistantConflict, AutomationStore
from lumen.config import LimitsConfig
from lumen.runtime import AgentRuntime


def test_goal_creation_retry_preserves_saved_goal_and_execution(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "assistant.db")
    store.open()
    try:
        creation = AssistantEdit(action="goal", id="stable", source_session_id="original",
                                 goal=GoalInput(title="Plan once", intent="Keep the original scope"))
        store.edit(creation)
        saved = store.get("goal", "stable", Goal)
        store.edit(creation)
        assert store.get("goal", "stable", Goal) == saved
        store.control_goal(GoalCommand(action="start", goal_id="stable", request_id="activation",
                                       expected_revision=saved.revision, criteria="A verified result"))
        active = store.get("goal", "stable", Goal)
        store.edit(creation)
        assert store.get("goal", "stable", Goal) == active
        assert active.primary_session_id == "original"
    finally:
        store.close()


def test_output_feed_is_versioned_idempotent_and_respects_deleted_resources(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "assistant.db")
    store.open()
    try:
        store.edit(AssistantEdit(action="goal", id="goal", goal=GoalInput(title="Ship a report")))
        resource = Resource(id="file", title="Report.md", kind="file", artifact_ref="first",
                            session_id="discussion", created_at=datetime.now(UTC), file_path="Report.md")
        for _ in range(2):
            store.collect_file(resource, goal_ids=("goal",))
        first = store.feed()
        assert len(first) == 1
        assert (first[0].resource_id, first[0].resource_revision, first[0].goal_id,
                first[0].session_id) == ("file", 1, "goal", "discussion")
        updated = store.collect_file(
            resource.model_copy(update={"artifact_ref": "second"}), goal_ids=("goal",),
        )
        assert [item.resource_revision for item in store.feed()] == [2, 1]
        store.record_output("file", goal_ids=("goal",))
        assert len(store.feed()) == 2
        store.edit(AssistantEdit(action="rename_resource", id="file", expected_revision=updated.revision,
                                 title="Renamed report.md"))
        store.record_output("file", goal_ids=("goal",))
        assert len(store.feed()) == 2
        store.edit(AssistantEdit(action="trash_resource", id="file", expected_revision=updated.revision + 1))
        deleted = store.collect_file(
            resource.model_copy(update={"artifact_ref": "third"}), goal_ids=("goal",),
        )
        assert not store.feed()
        with pytest.raises(ValueError, match="recycle bin"):
            store.resource_version("file", 1)
        store.edit(AssistantEdit(action="restore_resource", id="file", expected_revision=deleted.revision))
        assert store.resource_version("file", 1).artifact_ref == "first"
        assert len(store.feed()) == 2
    finally:
        store.close()


def test_goal_attention_follows_current_activation_and_keeps_history(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "assistant.db")
    store.open()
    try:
        store.edit(AssistantEdit(action="goal", id="goal", goal=GoalInput(title="Verify result")))
        for activation, outcome in (
            ("first", GoalExecutionStatus.BLOCKED), ("second", GoalExecutionStatus.REVIEW),
        ):
            goal = store.get("goal", "goal", Goal)
            store.control_goal(GoalCommand(action="start", goal_id=goal.id, request_id=activation,
                                           expected_revision=goal.revision, criteria="A verified file"))
            assert store.page("feed", None, "", "attention", 10)["items"] == []
            store.admit_goal_run("goal", activation, f"run-{activation}")
            for _ in range(2):
                store.finish_goal_run(
                    "goal", activation, outcome, "Review this result", run_id=f"run-{activation}",
                )
            assert len(store.page("feed", None, "", "attention", 10)["items"]) == 1
        goal = store.get("goal", "goal", Goal)
        store.edit(AssistantEdit(action="goal", id=goal.id, expected_revision=goal.revision,
                                 goal=GoalInput(title=goal.title, status="completed")))
        assert store.page("feed", None, "", "attention", 10)["items"] == []
        assert len(store.feed()) == 2
    finally:
        store.close()


@pytest.mark.parametrize("target_kind", ["goal", "resource"])
def test_feed_hides_unavailable_targets_before_pagination_and_restores_after_restart(
    tmp_path: Path, target_kind: str,
) -> None:
    store = AutomationStore(tmp_path / "assistant.db")
    store.open()
    try:
        now = datetime.now(UTC)
        for identity in ("keep", "target"):
            store.edit(AssistantEdit(action="goal", id=identity, goal=GoalInput(title=identity)))
        independent = FeedItem(sequence=0, id="independent", title="Needs a decision",
                               result=Result.NEEDS_USER_ACTION, detail="Choose next step", created_at=now)
        with store.transaction() as db:
            db.execute("INSERT INTO feed(id,body) VALUES(?,?)",
                       (independent.id, independent.model_dump_json()))
        for identity in ("keep", "target"):
            store.publish(Resource(id=identity, title=identity, kind="note", artifact_ref="body",
                                   created_at=now), goal_id=identity)
        store.edit(AssistantEdit(action="read", id="publish:keep", value=True))
        blocked = independent.model_copy(update={"id": "blocked", "goal_id": "target",
                                                  "resource_id": "target"})
        dangling = independent.model_copy(update={"id": "dangling", f"{target_kind}_id": "missing"})
        with store.transaction() as db:
            for item in (blocked, dangling):
                db.execute("INSERT INTO feed(id,body) VALUES(?,?)", (item.id, item.model_dump_json()))
        original_ids = ["blocked", "publish:target", "publish:keep", "independent"]
        assert [item.id for item in store.feed()] == original_ids
        store.edit(AssistantEdit(action="trash_goal" if target_kind == "goal" else "trash_resource",
                                 id="target", expected_revision=1))
        store.close()
        store.open()
        assert [item.id for item in store.feed()] == ["publish:keep", "independent"]
        assert [item.id for item in store.snapshot(True, "running", ()).feed] == [
            "publish:keep", "independent",
        ]
        first = store.page("feed", None, "", "all", 1)
        assert [item["id"] for item in first["items"]] == ["publish:keep"]
        second = store.page("feed", first["next_cursor"], "", "all", 1)
        assert [item["id"] for item in second["items"]] == ["independent"]
        assert second["next_cursor"] is None
        for category in ("attention", "unread"):
            assert [item["id"] for item in store.page("feed", None, "", category, 1)["items"]] == [
                "independent"
            ]
        assert store.page("feed", None, "target", "all", 10)["items"] == []
        store.edit(AssistantEdit(action="trash_resource" if target_kind == "goal" else "trash_goal",
                                 id="target", expected_revision=1))
        store.edit(AssistantEdit(action="restore_goal" if target_kind == "goal" else "restore_resource",
                                 id="target", expected_revision=2))
        assert [item.id for item in store.feed()] == ["publish:keep", "independent"]
        store.edit(AssistantEdit(action="restore_resource" if target_kind == "goal" else "restore_goal",
                                 id="target", expected_revision=2))
        assert [item.id for item in store.feed()] == original_ids
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM feed").fetchone()[0] == 5
    finally:
        store.close()


async def test_feed_api_and_overview_respect_recycle_bin_without_erasing_history(tmp_path: Path) -> None:
    host = await WorkspaceHost(resources_for(tmp_path, strict_background=False)).open()  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="feed-test", api_only=True)
    try:
        await host.dispatch(EditAssistant(AssistantEdit(
            action="goal", id="goal", goal=GoalInput(title="Goal", milestones=(
                Milestone(id="next", title="Review"),
            )),
        )))
        host.automations.store.publish(Resource(id="output", title="Output", kind="note", artifact_ref="body",
                                               created_at=datetime.now(UTC)), goal_id="goal")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            await client.get("/auth/exchange?token=feed-test")
            assert len((await client.get("/api/v1/assistant/feed")).json()["items"]) == 1
            await host.dispatch(EditAssistant(AssistantEdit(
                action="trash_goal", id="goal", expected_revision=1,
            )))
            for category in ("all", "unread", "attention"):
                assert (await client.get(f"/api/v1/assistant/feed?category={category}")).json()["items"] == []
            state = (await client.get("/api/v1/assistant")).json()
            assert state["feed"] == []
            assert state["goals"][0]["deleted_at"] is not None
            assert all(ref["kind"] != "goal" for idea in state["ideas"] for ref in idea["context_refs"])
            await host.dispatch(EditAssistant(AssistantEdit(
                action="restore_goal", id="goal", expected_revision=2,
            )))
            assert len((await client.get("/api/v1/assistant/feed")).json()["items"]) == 1
            restored = (await client.get("/api/v1/assistant")).json()
            assert any(idea["context_refs"] for idea in restored["ideas"])
    finally:
        await host.close()


def test_new_follow_up_results_do_not_link_to_deleted_goals(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "assistant.db")
    store.open()
    try:
        now = datetime.now(UTC)
        identity = store.command(AutomationCommand(action="create", request_id="new",
            spec=AutomationInput(name="Follow", intent="Read", paths=("input.md",))), "fp", now)
        store.edit(AssistantEdit(action="goal", id="old", goal=GoalInput(
            title="Old", automation_ids=(identity,),
        )))
        store.edit(AssistantEdit(action="trash_goal", id="old", expected_revision=1))
        trial = store.command(AutomationCommand(action="trial", id=identity, request_id="trial",
                                               expected_revision=1), "fp", now)
        store.settle(store.get("occurrence", trial, Occurrence).model_copy(update={
            "result": Result.COMPLETE, "fingerprint": "fp",
        }))
        run = store.command(AutomationCommand(action="run", id=identity, request_id="run",
                                             expected_revision=1), "fp", now)
        store.settle(store.get("occurrence", run, Occurrence).model_copy(update={
            "result": Result.COMPLETE, "artifact_ref": "body",
        }))
        assert store.feed()[0].goal_id is None
        assert store.resource_links(run)["goal_ids"] == []
    finally:
        store.close()


def test_recycle_bin_and_collection_changes_preserve_content_and_relationships(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "assistant.db")
    store.open()
    try:
        store.edit(AssistantEdit(action="goal", id="goal", goal=GoalInput(title="Goal")))
        store.edit(AssistantEdit(action="note", id="note", title="Note", content="body"), artifact_ref="body")
        store.edit(AssistantEdit(action="collection", id="collection", title="Collection"))
        store.edit(AssistantEdit(action="collect", id="note", collection_id="collection"))
        store.edit(AssistantEdit(action="link_resource", id="note", goal_id="goal", role="output"))
        with pytest.raises(AssistantConflict):
            store.edit(AssistantEdit(
                action="rename_resource", id="note", title="Renamed", expected_revision=0,
            ))
        store.edit(AssistantEdit(action="rename_resource", id="note", title="Renamed", expected_revision=1))
        assert store.resource_version("note", 1).title == "Note"
        store.edit(AssistantEdit(action="trash_resource", id="note", expected_revision=2))
        store.edit(AssistantEdit(action="trash_resource", id="note", expected_revision=2))
        assert store.page("resource", None, "", "all", 50)["items"] == []
        assert store.page("resource", None, "", "collection:collection", 50)["items"] == []
        assert len(store.page("resource", None, "", "trash", 50)["items"]) == 1
        assert store.goal_detail("goal").resources == ()
        with pytest.raises(ValueError, match="recycle bin"):
            store.resource_version("note", 1)
        store.edit(AssistantEdit(action="restore_resource", id="note", expected_revision=3))
        assert store.goal_detail("goal").resources[0].artifact_ref == "body"
        assert store.resource_links("note")["collection_ids"] == ["collection"]
        store.edit(AssistantEdit(action="delete_collection", id="collection", expected_revision=1))
        assert store.resource_version("note", None).title == "Renamed"
        assert store.resource_links("note")["collection_ids"] == []
        store.edit(AssistantEdit(action="trash_goal", id="goal", expected_revision=1))
        with pytest.raises(ValueError, match="recycle bin"):
            store.discussion(ContextRef(kind="goal", id="goal"))
        with pytest.raises(ValueError, match="Restore"):
            store.edit(AssistantEdit(
                action="goal", id="goal", goal=GoalInput(title="Goal"), expected_revision=2,
            ))
        store.edit(AssistantEdit(action="restore_goal", id="goal", expected_revision=2))
        assert store.goal_detail("goal").resources[0].title == "Renamed"
        file = Resource(id="file", title="file.pdf", kind="file", artifact_ref="binary",
                        file_path="file.pdf", created_at=datetime.now(UTC))
        store.import_file(file, text="")
        with pytest.raises(ValueError, match="TaskWorkspace"):
            store.edit(AssistantEdit(action="edit_resource", id="file", title="oops", content="text",
                                     expected_revision=1), artifact_ref="text")
    finally:
        store.close()


async def test_import_files_is_authenticated_bounded_idempotent_and_versioned(tmp_path: Path) -> None:
    host = await WorkspaceHost(resources_for(tmp_path, strict_background=False)).open()  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="management-test", api_only=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            identity = str(uuid4())
            path = f"/api/v1/assistant/resources/import?filename=image.png&identity={identity}"
            content = b"\x89PNG\r\n\x1a\n\x00\xff"
            assert (await client.post(path, content=content)).status_code == 401
            await client.get("/auth/exchange?token=management-test")
            assert (await client.post(path, content=content)).status_code == 403
            client.headers["origin"] = "http://testserver"
            result = await client.post(path, content=content)
            assert result.status_code == 200
            resource = Resource.model_validate(result.json())
            assert resource.file_type == "image" and resource.byte_size == len(content)
            assert (await host.read_resource_document(resource.id))[1] == content
            assert (await client.post(path, content=content)).json()["id"] == resource.id
            assert (await client.post(path, content=b"different")).status_code == 409
            unsafe = f"/api/v1/assistant/resources/import?filename=../private.txt&identity={uuid4()}"
            assert (await client.post(unsafe, content=b"no")).status_code >= 400
            with pytest.raises(InvalidStateError, match="20 MiB"):
                await host.import_resource_file("large.pdf", b"x" * (20 * 1024 * 1024 + 1), str(uuid4()))
            await host.dispatch(EditAssistant(AssistantEdit(
                action="trash_resource", id=resource.id, expected_revision=1,
            )))
            with pytest.raises(InvalidStateError):
                await host.read_resource_document(resource.id, 1)
            await host.dispatch(EditAssistant(AssistantEdit(
                action="restore_resource", id=resource.id, expected_revision=2,
            )))
            assert (await host.dispatch(ReadAssistantResource(resource.id))).data["content"] == ""
            assert len(host.automations.store.page("resource", None, "", "all", 50)["items"]) == 1
    finally:
        await host.close()


async def test_goal_deletion_requires_foreground_and_follow_up_runs_to_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    finishing = asyncio.Event()
    finish_release = asyncio.Event()
    model_finished = False

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal model_finished
        await release.wait()
        model_finished = True
        yield "Completed"

    resources = resources_for(tmp_path, strict_background=False)
    resources.runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[], toolsets=[],
        instructions="Test goal management", limits=LimitsConfig(), tool_metadata={},
    )
    host = await WorkspaceHost(resources).open()  # type: ignore[arg-type]
    sync = host._sync_assistant_products  # pyright: ignore[reportPrivateUsage]

    async def delayed_sync(session_id: str) -> None:
        if model_finished:
            finishing.set()
            await finish_release.wait()
        await sync(session_id)

    monkeypatch.setattr(host, "_sync_assistant_products", delayed_sync)
    try:
        await host.dispatch(EditAssistant(AssistantEdit(
            action="goal", id="goal", goal=GoalInput(title="Goal"),
        )))
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "Work", "run",
                                              context_refs=(ContextRef(kind="goal", id="goal"),)))
        with pytest.raises(ConfigurationConflictHostError, match="current run"):
            await host.dispatch(EditAssistant(AssistantEdit(
                action="trash_goal", id="goal", expected_revision=1,
            )))
        release.set()
        await finishing.wait()
        with pytest.raises(ConfigurationConflictHostError, match="current run"):
            await host.dispatch(EditAssistant(AssistantEdit(
                action="trash_goal", id="goal", expected_revision=1,
            )))
        finish_release.set()
        _ = [event async for event in host.subscribe(started.run_id)]
        store = host.automations.store
        now = datetime.now(UTC)
        automation = store.command(AutomationCommand(action="create", request_id="follow",
            spec=AutomationInput(name="Follow", intent="Read", paths=("a.md",))), "fp", now)
        await host.dispatch(EditAssistant(AssistantEdit(action="goal", id="goal", expected_revision=1,
            goal=GoalInput(title="Goal", automation_ids=(automation,)))))
        store.command(AutomationCommand(action="trial", id=automation, request_id="trial",
                                        expected_revision=1), "fp", now)
        with pytest.raises(ConfigurationConflictHostError, match="Pause linked"):
            await host.dispatch(EditAssistant(AssistantEdit(
                action="trash_goal", id="goal", expected_revision=2,
            )))
    finally:
        release.set()
        finish_release.set()
        await host.close()
