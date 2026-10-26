from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from test_workspace_host import LocalResources

from lumen.api import create_web_app
from lumen.application import (
    ApplyAutomationCommand,
    ApplyPreferenceCommand,
    ConfigurationConflictHostError,
    CreateSession,
    EditAssistant,
    GetAssistantPreferences,
    GetAssistantState,
    InvalidStateError,
    ReadAssistantResource,
    StartRun,
    WorkspaceHost,
)
from lumen.application.run_lock import WorkspaceRunLock
from lumen.automation.models import (
    AssistantEdit,
    AssistantState,
    AutomationCommand,
    AutomationInput,
    Definition,
    GoalInput,
    Milestone,
    Occurrence,
    Phase,
    PreferenceCommand,
    Resource,
    Result,
    Schedule,
)
from lumen.automation.store import AssistantConflict, AutomationStore
from lumen.config import LimitsConfig, PermissionsConfig
from lumen.context.memory import MemoryManager
from lumen.context.memory.records import MemorySource, MemoryStatus
from lumen.context.memory.repository import InMemoryMemoryRepository
from lumen.runtime import AgentRuntime
from lumen.tools.builtin import build_builtin_specs
from lumen.tools.gateway import CapabilityDescriptor, CapabilityGateway
from lumen.tools.registry import PermissionPolicy, ToolRegistry
from lumen.tools.spec import EffectKind
from lumen.work_products import TaskWorkspace


def test_schedule_dst_and_input_boundaries() -> None:
    gap = Schedule(kind="daily", timezone="America/New_York", hour=2, minute=30)
    assert gap.next_after(datetime(2026, 3, 8, 0, tzinfo=UTC)) == datetime(2026, 3, 8, 7, tzinfo=UTC)
    fold = Schedule(kind="daily", timezone="America/New_York", hour=1, minute=30)
    assert fold.next_after(datetime(2026, 11, 1, 0, tzinfo=UTC)) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    for path in ["../secret", "/tmp/private", ".env", "src/../key", "a\\b"]:
        with pytest.raises(ValidationError):
            AutomationInput(name="test", intent="test", paths=(path,))


def test_durable_request_cas_and_pause(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "state.db")
    store.open()
    now = datetime(2026, 10, 20, 8, tzinfo=UTC)
    command = AutomationCommand(
        request_id="create",
        action="create",
        spec=AutomationInput(
            name="Review",
            intent="Summarize",
            paths=("README.md",),
        ),
    )
    identity = store.command(command, "fp", now)
    assert store.command(command, "fp", now) == identity
    with pytest.raises(AssistantConflict):
        store.command(command.model_copy(update={"action": "pause"}), "fp", now)
    definition = store.get("definition", identity, Definition)
    trial_id = store.command(
        AutomationCommand(
            request_id="trial",
            action="trial",
            id=identity,
            expected_revision=1,
        ),
        "fp",
        now,
    )
    trial = store.get("occurrence", trial_id, Occurrence)
    store.settle(trial.model_copy(update={"result": Result.COMPLETE, "fingerprint": "fp"}))
    store.command(
        AutomationCommand(
            request_id="enable",
            action="enable",
            id=identity,
            expected_revision=1,
        ),
        "fp",
        now,
    )
    store.materialize(definition.spec.schedule.next_after(now))
    assert len(store.pending()) == 1
    store.command(
        AutomationCommand(
            request_id="pause",
            action="pause",
            id=identity,
            expected_revision=2,
        ),
        "fp",
        now,
    )
    assert store.pending() == ()
    store.close()
    reopened = AutomationStore(tmp_path / "state.db")
    reopened.open()
    assert reopened.command(command, "fp", now) == identity
    assert not reopened.get("definition", identity, Definition).enabled
    reopened.close()


def test_control_projection_keeps_old_definitions_accessible(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path / "state.db")
    store.open()
    try:
        for index in range(201):
            store.command(
                AutomationCommand(
                    request_id=f"draft-{index}",
                    action="create",
                    spec=AutomationInput(
                        name=f"Draft {index}",
                        intent="Review",
                        paths=("README.md",),
                    ),
                ),
                "fp",
                datetime.now(UTC),
            )
        assert len(store.snapshot(True, "running", ()).definitions) == 201
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["missing_checkpoint_table", "unversioned_database"])
def test_sqlite_layout_damage_is_not_silently_repaired(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "state.db"
    store = AutomationStore(path)
    if damage == "missing_checkpoint_table":
        store.open()
        store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DROP TABLE checkpoints"
            if damage == "missing_checkpoint_table"
            else "CREATE TABLE unrelated(value TEXT)"
        )
    try:
        for _ in range(2):
            with pytest.raises(ValueError, match="refusing to recreate state"):
                store.open()
    finally:
        store.close()
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "checkpoints" not in tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == (
            2 if damage == "missing_checkpoint_table" else 0
        )


def resources_for(tmp_path: Path, *, strict_background: bool = True) -> LocalResources:
    async def stream(_messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        if strict_background:
            assert not info.function_tools
        yield "A source-backed project review."

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="Summarize",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    resources = LocalResources(tmp_path, runtime)
    registry = ToolRegistry(tmp_path)
    registry.add_many(build_builtin_specs(tmp_path), origin="builtin")
    resources.capability_gateway = CapabilityGateway(
        registry,
        PermissionPolicy(PermissionsConfig()),
        default_timeout=10,
    )
    return resources


async def test_startup_recovery_failure_releases_owner_and_allows_host_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = WorkspaceHost(resources_for(tmp_path, strict_background=False))  # type: ignore[arg-type]
    contender = WorkspaceRunLock(tmp_path, name="automation")

    def fail_pending() -> tuple[Occurrence, ...]:
        raise ValueError("Invalid persisted occurrence")

    monkeypatch.setattr(host.automations.store, "pending", fail_pending)
    await host.open()
    try:
        assert host.automations.status == "storage_error"
        assert not host.automations.owner
        assert contender.acquire()
        session = await host.dispatch(CreateSession())
        run = await host.dispatch(StartRun(session.session_id, "An ordinary question", "interactive"))
        assert [event async for event in host.subscribe(run.run_id)][-1].type == "run.completed"
    finally:
        contender.release()
        await host.close()


def test_owner_can_stop_during_wakeup_and_reopen_on_a_new_event_loop(tmp_path: Path) -> None:
    host = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]

    async def cycle() -> None:
        async with asyncio.timeout(2):
            await host.open()
            await host.dispatch(
                ApplyAutomationCommand(
                    AutomationCommand(
                        request_id="same-draft",
                        action="create",
                        spec=AutomationInput(
                            name="Review",
                            intent="Summarize",
                            paths=("README.md",),
                        ),
                    )
                )
            )
            await host.close()
        assert not host.automations.owner

    asyncio.run(cycle())
    asyncio.run(cycle())


async def wait_settled(host: WorkspaceHost, identity: str) -> AssistantState:
    async with asyncio.timeout(10):
        while True:
            state = AssistantState.model_validate((await host.dispatch(GetAssistantState())).data)
            occurrence = next((item for item in state.occurrences if item.id == identity), None)
            if occurrence is not None and occurrence.phase is Phase.SETTLED:
                return state
            await asyncio.sleep(0.01)  # The durable state is the assertion, not a mocked timer callback.


@pytest.mark.skipif(sys.platform == "win32", reason="Descriptor-safe source reads require POSIX")
@pytest.mark.parametrize("privacy_mode", ["disabled", "incognito"])
async def test_host_trial_manual_delivery_resources_and_goals(tmp_path: Path, privacy_mode: str) -> None:
    (tmp_path / "README.md").write_text("# Project\nA changed project document.")
    resources = resources_for(tmp_path)
    manager = MemoryManager(InMemoryMemoryRepository(), project_id="project", use=privacy_mode != "disabled")
    private_preference = "Prefer summaries with exactly three bullet points"
    manager.remember(private_preference)
    manager.incognito = privacy_mode == "incognito"
    resources.memory_manager = manager  # type: ignore[attr-defined]
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="create",
                    action="create",
                    spec=AutomationInput(
                        name="项目简报",
                        intent="Summarize project changes",
                        paths=("README.md",),
                    ),
                )
            )
        )
        identity = cast(str, created.data["id"])
        trial = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="trial",
                    action="trial",
                    id=identity,
                    expected_revision=1,
                )
            )
        )
        state = await wait_settled(host, cast(str, trial.data["id"]))
        assert state.definitions[0].validation is not None
        assert state.feed == ()
        trial_session = resources.session_repository.load(state.occurrences[0].session_id)
        assert private_preference not in str(trial_session.turns[0].messages)
        started = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="run",
                    action="run",
                    id=identity,
                    expected_revision=1,
                )
            )
        )
        occurrence_id = cast(str, started.data["id"])
        state = await wait_settled(host, occurrence_id)
        assert state.occurrences[0].result is Result.COMPLETE
        assert len(state.feed) == 1
        content = await host.dispatch(ReadAssistantResource(occurrence_id))
        assert "source-backed" in content.data["content"]
        original = content.data["content"]
        await host.dispatch(
            EditAssistant(
                AssistantEdit(
                    action="edit_resource",
                    id=occurrence_id,
                    expected_revision=1,
                    title="Reviewed report",
                    content="A user-reviewed version of the report.",
                )
            )
        )
        assert (await host.dispatch(ReadAssistantResource(occurrence_id))).data["resource"]["revision"] == 2
        assert (await host.dispatch(ReadAssistantResource(occurrence_id, historical=True))).data[
            "content"
        ] == original
        with pytest.raises(ConfigurationConflictHostError):
            await host.dispatch(
                EditAssistant(
                    AssistantEdit(
                        action="edit_resource",
                        id=occurrence_id,
                        expected_revision=1,
                        title="A stale editor",
                        content="Do not overwrite the newer version.",
                    )
                )
            )
        await host.dispatch(EditAssistant(AssistantEdit(action="save", id=occurrence_id)))
        await host.dispatch(
            EditAssistant(
                AssistantEdit(
                    action="goal",
                    id="goal-one",
                    goal=GoalInput(
                        title="Project",
                        milestones=(Milestone(id="one", title="Review", done=True),),
                        automation_ids=(identity,),
                    ),
                )
            )
        )
        state = AssistantState.model_validate((await host.dispatch(GetAssistantState())).data)
        assert next(resource for resource in state.resources if resource.id == occurrence_id).saved
        assert state.goals[0].milestones[0].done
        second = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="run-again",
                    action="run",
                    id=identity,
                    expected_revision=1,
                )
            )
        )
        state = await wait_settled(host, cast(str, second.data["id"]))
        assert state.occurrences[0].result is Result.COMPLETE
        assert len(state.feed) == 2
        assert host.resources.runtime is not None
        assert host.resources.runtime.limits.request_count is None
    finally:
        await host.close()


async def test_owner_and_no_transparent_restart(tmp_path: Path) -> None:
    first = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    second = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await first.open()
    await second.open()
    try:
        assert first.automations.owner
        assert not second.automations.owner
    finally:
        await second.close()
        await first.close()


async def test_recovery_before_session_creation_never_replays(tmp_path: Path) -> None:
    first = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await first.open(automation_owner=False)
    now = datetime.now(UTC)
    store = first.automations.store
    identity = store.command(
        AutomationCommand(
            request_id="create",
            action="create",
            spec=AutomationInput(
                name="review",
                intent="summarize",
                paths=("README.md",),
            ),
        ),
        "fingerprint",
        now,
    )
    trial_id = store.command(
        AutomationCommand(
            request_id="trial",
            action="trial",
            id=identity,
            expected_revision=1,
        ),
        "fingerprint",
        now,
    )
    trial = store.get("occurrence", trial_id, Occurrence)
    store.begin(trial, "fingerprint", now)
    await first.close()
    restarted = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await restarted.open()
    try:
        state = AssistantState.model_validate((await restarted.dispatch(GetAssistantState())).data)
        assert state.occurrences[0].result is Result.INTERRUPTED
        assert restarted.resources.session_repository.list() == []
    finally:
        await restarted.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Descriptor-safe source reads require POSIX")
async def test_settlement_failure_recovers_without_a_second_model_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "README.md").write_text("# A project to review")
    host = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await host.open()
    identity = (
        await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    action="create",
                    request_id="create",
                    spec=AutomationInput(
                        name="Review",
                        intent="summarize",
                        paths=("README.md",),
                    ),
                )
            )
        )
    ).data["id"]
    original = host.automations.store.settle
    failed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def fail_commit(_occurrence: Occurrence, _text: str = "") -> None:
        loop.call_soon_threadsafe(failed.set)
        raise OSError("injected storage failure")

    monkeypatch.setattr(host.automations.store, "settle", fail_commit)
    started = await host.dispatch(
        ApplyAutomationCommand(
            AutomationCommand(
                action="trial",
                request_id="trial",
                id=identity,
                expected_revision=1,
            )
        )
    )
    await asyncio.wait_for(failed.wait(), timeout=10)
    pending = host.automations.store.get("occurrence", started.data["id"], Occurrence)
    assert pending.phase is Phase.SETTLING
    turns = host.resources.session_repository.load(pending.session_id).turns
    assert len(turns) == 1 and turns[0].status == "completed"
    monkeypatch.setattr(host.automations.store, "settle", original)
    await host.close()
    restarted = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await restarted.open()
    try:
        state = AssistantState.model_validate((await restarted.dispatch(GetAssistantState())).data)
        assert state.occurrences[0].phase is Phase.SETTLED
        assert state.definitions[0].validation
        assert len(restarted.resources.session_repository.load(pending.session_id).turns) == 1
        restarted.automations.store.settle(state.occurrences[0])
        assert len(restarted.automations.store.list("resource", Resource)) == 1
    finally:
        await restarted.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Descriptor-safe source reads require POSIX")
@pytest.mark.parametrize("source", ["missing.md", "link.md"])
async def test_missing_or_symlink_sources_cannot_succeed(tmp_path: Path, source: str) -> None:
    (tmp_path / "elsewhere.md").write_text("No implicit symlink permission")
    if source == "link.md":
        (tmp_path / "link.md").symlink_to(tmp_path / "elsewhere.md")
    host = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await host.open()
    try:
        identity = (
            await host.dispatch(
                ApplyAutomationCommand(
                    AutomationCommand(
                        request_id="create",
                        action="create",
                        spec=AutomationInput(
                            name="review",
                            intent="summarize",
                            paths=(source,),
                        ),
                    )
                )
            )
        ).data["id"]
        trial = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="trial",
                    action="trial",
                    id=identity,
                    expected_revision=1,
                )
            )
        )
        state = await wait_settled(host, trial.data["id"])
        assert state.occurrences[0].result is Result.NEEDS_USER_ACTION
        assert state.definitions[0].validation is None
        assert state.resources == ()
        assert not state.definitions[0].enabled
    finally:
        await host.close()


async def test_corrupt_automation_storage_does_not_start_jobs_or_break_chat(tmp_path: Path) -> None:
    host = WorkspaceHost(resources_for(tmp_path, strict_background=False))  # type: ignore[arg-type]
    path = host.automations.store.path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a SQLite database")
    await host.open()
    try:
        state = AssistantState.model_validate((await host.dispatch(GetAssistantState())).data)
        assert state.status == "storage_error"
        assert host.automations.status == "storage_error"
        assert path.read_bytes() == b"not a SQLite database"
        session = await host.dispatch(CreateSession())
        run = await host.dispatch(StartRun(session.session_id, "An ordinary question", "interactive"))
        events = [event async for event in host.subscribe(run.run_id)]
        assert events[-1].type == "run.completed"
    finally:
        await host.close()


def test_api_auth_strict_validation_and_saved_state(tmp_path: Path) -> None:
    host = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    app = create_web_app(host, launch_token="fixture-token", api_only=True)
    with TestClient(app) as client:
        assert client.get("/api/v1/assistant").status_code == 401
        client.get("/auth/exchange", params={"token": "fixture-token"})
        client.headers["origin"] = "http://testserver"
        assert client.get("/api/v1/assistant").status_code == 200
        endpoint = "/api/v1/assistant/automations"
        payload = {
            "action": "create",
            "request_id": "one",
            "spec": {
                "name": "Review",
                "intent": "Summarize",
                "paths": ["../escape"],
            },
        }
        rejected = client.post(endpoint, json=payload)
        assert rejected.status_code == 422
        assert "input" not in rejected.json()["error"]["details"][0]
        payload["spec"]["paths"] = ["README.md"]  # type: ignore[index]
        saved = client.post(endpoint, json=payload)
        assert saved.status_code == 202
        assert client.post(endpoint, json=payload).json() == saved.json()
        assert len(client.get("/api/v1/assistant").json()["definitions"]) == 1
        assert client.post(endpoint, json={**payload, "unknown": True}).status_code == 422
        assert (
            client.post(
                "/api/v1/assistant/state",
                json={
                    "action": "note",
                    "id": "note-one",
                    "title": "A note",
                    "content": "Inspectable content",
                },
            ).status_code
            == 200
        )
        assert "Inspectable content" in client.get("/api/v1/assistant/resources/note-one").json()["content"]
        for identity in ("note-two", "note-three"):
            assert (
                client.post(
                    "/api/v1/assistant/state",
                    json={
                        "action": "note",
                        "id": identity,
                        "title": identity,
                        "content": "A second note",
                    },
                ).status_code
                == 200
            )
        page = client.get("/api/v1/assistant/resources", params={"limit": 1}).json()
        assert page["items"][0]["id"] == "note-three"
        older = client.get(
            "/api/v1/assistant/resources", params={"limit": 1, "before": page["next_cursor"]}
        ).json()
        assert older["items"][0]["id"] == "note-two"
        searched = client.get("/api/v1/assistant/resources", params={"query": "A note"}).json()
        assert [item["id"] for item in searched["items"]] == ["note-one"]


@pytest.mark.skipif(sys.platform == "win32", reason="Descriptor-safe source reads require POSIX")
async def test_background_cannot_add_an_absent_parent_read_capability(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Readable text is still outside the granted tool set")
    resources = resources_for(tmp_path)
    resources.capability_gateway = CapabilityGateway(
        ToolRegistry(tmp_path),
        PermissionPolicy(PermissionsConfig()),
        default_timeout=10,
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        identity = (
            await host.dispatch(
                ApplyAutomationCommand(
                    AutomationCommand(
                        request_id="create",
                        action="create",
                        spec=AutomationInput(
                            name="Review",
                            intent="summarize",
                            paths=("README.md",),
                        ),
                    )
                )
            )
        ).data["id"]
        trial = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="trial",
                    action="trial",
                    id=identity,
                    expected_revision=1,
                )
            )
        )
        state = await wait_settled(host, trial.data["id"])
        assert state.occurrences[0].result is Result.NEEDS_USER_ACTION
        assert state.resources == ()
        assert all(receipt.artifact_ref is None for receipt in state.occurrences[0].receipts)
    finally:
        await host.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Descriptor-safe source reads require POSIX")
@pytest.mark.parametrize(("origin", "confirmation"), [("builtin", True), ("plugin", False)])
async def test_source_profile_does_not_replace_confirmed_or_custom_readers(
    tmp_path: Path,
    origin: str,
    confirmation: bool,
) -> None:
    (tmp_path / "README.md").write_text("The file exists but is outside this source contract")
    resources = resources_for(tmp_path)
    gateway = CapabilityGateway(
        ToolRegistry(tmp_path),
        PermissionPolicy(PermissionsConfig()),
        default_timeout=10,
    )

    async def unexpected_read(_arguments: dict[str, object]) -> str:
        pytest.fail("The original reader must not execute")

    gateway.register(
        CapabilityDescriptor(
            name="read_file",
            parameters={"type": "object"},
            origin=origin,
            risk="read",
            effect_kind=EffectKind.OBSERVE,
            requires_approval=confirmation,
            timeout_seconds=10,
        ),
        unexpected_read,
    )
    resources.capability_gateway = gateway
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="draft",
                    action="create",
                    spec=AutomationInput(
                        name="Review",
                        intent="Summarize",
                        paths=("README.md",),
                    ),
                )
            )
        )
        started = await host.dispatch(
            ApplyAutomationCommand(
                AutomationCommand(
                    request_id="trial",
                    action="trial",
                    id=created.data["id"],
                    expected_revision=1,
                )
            )
        )
        state = await wait_settled(host, started.data["id"])
        assert state.occurrences[0].result is Result.NEEDS_USER_ACTION
        assert state.definitions[0].validation is None
        assert state.resources == ()
    finally:
        await host.close()


async def test_preferences_use_existing_authority_and_confirmation_preserves_identity(tmp_path: Path) -> None:
    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository, project_id="project")
    record = manager.remember("Prefer concise project summaries")
    candidate = record.model_copy(
        update={"source_kind": MemorySource.INFERRED, "status": MemoryStatus.CANDIDATE}
    )
    repository.remember(candidate)
    resources = resources_for(tmp_path)
    resources.memory_manager = manager  # type: ignore[attr-defined]
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        await host.dispatch(
            ApplyPreferenceCommand(
                PreferenceCommand(
                    action="confirm",
                    id=record.id,
                    expected_updated_at=record.updated_at.isoformat(),
                )
            )
        )
        current = manager.list()[0]
        assert current.id == record.id
        assert current.source_kind is MemorySource.EXPLICIT
        assert current.status is MemoryStatus.ACTIVE
        await host.dispatch(
            ApplyPreferenceCommand(
                PreferenceCommand(
                    action="forget",
                    id=current.id,
                    expected_updated_at=current.updated_at.isoformat(),
                )
            )
        )
        assert (await host.dispatch(GetAssistantPreferences())).data["records"] == []
        with pytest.raises(InvalidStateError):
            await host.dispatch(
                ApplyPreferenceCommand(
                    PreferenceCommand(
                        action="remember",
                        content=record.content,
                    )
                )
            )
    finally:
        await host.close()


async def test_library_indexes_owned_work_products_and_does_not_copy_their_body(tmp_path: Path) -> None:
    (tmp_path / "result.md").write_text("Canonical work product content")
    resources = resources_for(tmp_path, strict_background=False)
    resources.task_workspace = TaskWorkspace(tmp_path, resources.artifact_store, resources.session_repository)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        resources.task_workspace.bind_session(session.session_id)
        resources.task_workspace.open_work_product("result.md")
        started = await host.dispatch(StartRun(session.session_id, "Review this work product", "review"))
        _ = [event async for event in host.subscribe(started.run_id)]
        state = AssistantState.model_validate((await host.dispatch(GetAssistantState())).data)
        product = next(item for item in state.resources if item.kind == "work_product")
        body = await host.dispatch(ReadAssistantResource(product.id))
        assert body.data["content"] == "Canonical work product content"
        assert not (host.automations.artifacts.root / product.artifact_ref[7:]).exists()
        await host.dispatch(EditAssistant(AssistantEdit(
            action="rename_resource", id=product.id, title="Renamed output", expected_revision=1,
        )))
        await host.dispatch(EditAssistant(AssistantEdit(
            action="trash_resource", id=product.id, expected_revision=2,
        )))
        started = await host.dispatch(StartRun(session.session_id, "Review again", "review-again"))
        _ = [event async for event in host.subscribe(started.run_id)]
        items = host.automations.store.page("resource", None, "", "trash", 50)["items"]
        assert len(items) == 1 and items[0]["title"] == "Renamed output"
        assert host.automations.store.page("resource", None, "", "all", 50)["items"] == []
        with pytest.raises(InvalidStateError, match="recycle bin"):
            await host.dispatch(ReadAssistantResource(product.id, revision=1))
        await host.dispatch(EditAssistant(AssistantEdit(
            action="restore_resource", id=product.id, expected_revision=3,
        )))
        restored = await host.dispatch(ReadAssistantResource(product.id))
        assert restored.data["content"] == body.data["content"]
    finally:
        await host.close()
