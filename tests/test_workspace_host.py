from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import BinaryContent, ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.agents import AgentConfigSnapshot, AgentThreadRef, AgentThreadState
from lumen.agents.types import AgentStatus
from lumen.application import (
    ApprovalAlreadyResolvedError,
    ApprovePlan,
    CancelRun,
    ContextControl,
    CreateSession,
    DecideApproval,
    DeleteSession,
    EventEnvelope,
    ForkSessionAtTurn,
    InvalidStateError,
    InvokeSkill,
    ListSessions,
    RenameSession,
    RetryRun,
    SessionNotFoundError,
    SetCollaborationMode,
    SetSessionArchived,
    StartRun,
    StoreAttachment,
    WaivePlanVerification,
    WorkspaceBusyError,
    WorkspaceHost,
)
from lumen.application.run_lock import WorkspaceRunLock
from lumen.attachments import AttachmentStore
from lumen.config import LimitsConfig
from lumen.context import ArtifactStore
from lumen.plan import EvidenceKind, EvidenceReceipt, PlanState, PlanStep, StepStatus
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository
from lumen.skills import Skill
from lumen.tools.spec import EffectKind
from lumen.trust import ApprovalRuleStore
from lumen.work_products import EffectStatus, TaskWorkspace
from lumen.work_products.types import EffectReceipt


class LocalResources:
    def __init__(
        self,
        root: Path,
        runtime: AgentRuntime,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.workspace = root
        self.session_repository = SessionRepository(root / "sessions")
        self.artifact_store = artifact_store or ArtifactStore(root / "artifacts")
        self.runtime = runtime
        self.approval_rules = ApprovalRuleStore(root, state_root=root / "state")
        self.config = SimpleNamespace(
            agent=SimpleNamespace(name="test-agent"),
            permissions=SimpleNamespace(default_mode="manual"),
        )
        self.tool_metadata: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []
        self.skills: list[object] = []
        self.task_workspace: TaskWorkspace | None = None

    async def open(self) -> LocalResources:
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

    def summary(self) -> dict[str, object]:
        return {"agent": "test-agent", "model": "test-model"}

    def capabilities_report(self) -> dict[str, object]:
        return {"tools": [], "skills": [], "mcp_servers": [], "agent_profiles": []}

    def load_skill_by_name(self, name: str) -> object | None:
        return next((skill for skill in self.skills if getattr(skill, "name", None) == name), None)


def _runtime(output: str = "web ready") -> AgentRuntime:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield output

    return AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )


@pytest.mark.parametrize("action", ["complete", "rename", "rename_placeholder", "delete", "failure"])
async def test_title_generation_does_not_block_runs_or_override_user_management(
    tmp_path: Path, action: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    sources: list[str] = []

    class TitleResources(LocalResources):
        async def generate_session_title(self, input_text: str) -> str:
            sources.append(input_text)
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Even a provider returning after cancellation cannot overwrite a manual title.
                await release.wait()
            if action == "failure":
                raise TimeoutError("provider unavailable")
            return '"架构分享提纲"'

    resources = TitleResources(tmp_path, _runtime())
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(created.session_id, "请整理架构分享提纲", "title-request"))
        initial = (await host.dispatch(ListSessions())).sessions[0]
        assert (initial.title, initial.title_pending) == ("新对话", True)
        await asyncio.wait_for(entered.wait(), timeout=1)
        events = await asyncio.wait_for(_collect_run(host, started.run_id), timeout=2)
        assert events[-1].type == "run.completed"
        assert (await host.dispatch(ListSessions())).sessions[0].title_pending
        history = resources.session_repository.load(created.session_id).turns
        if action.startswith("rename"):
            await host.dispatch(RenameSession(
                created.session_id, "新对话" if action == "rename_placeholder" else "我的标题",
            ))
        elif action == "delete":
            await host.dispatch(DeleteSession(created.session_id))
        release.set()
        async with asyncio.timeout(1):
            # Metadata is observed through the public list contract, independently of run events.
            while any(item.title_pending for item in (await host.dispatch(ListSessions())).sessions):  # noqa: ASYNC110
                await asyncio.sleep(0)
        await host.close()
        result = (await host.dispatch(ListSessions())).sessions
        if action == "delete":
            assert result == []
        else:
            expected = {
                "complete": "架构分享提纲", "rename": "我的标题",
                "rename_placeholder": "新对话", "failure": "请整理架构分享提纲",
            }[action]
            assert (result[0].title, result[0].title_pending) == (expected, False)
        assert sources == ["请整理架构分享提纲"]
        assert resources.session_repository.load(created.session_id).turns == history
    finally:
        release.set()
        await host.close()


async def _collect_run(host: WorkspaceHost, run_id: str) -> list[EventEnvelope]:
    return [event async for event in host.subscribe(run_id)]


async def test_pending_title_recovers_from_recorded_input_after_host_restart(tmp_path: Path) -> None:
    entered = asyncio.Event()

    class InterruptedResources(LocalResources):
        async def generate_session_title(self, input_text: str) -> str:
            entered.set()
            await asyncio.Event().wait()
            return input_text

    first = WorkspaceHost(InterruptedResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await first.open()
    created = await first.dispatch(CreateSession())
    started = await first.dispatch(StartRun(created.session_id, "需要恢复的标题", "restart-title"))
    await _collect_run(first, started.run_id)
    await asyncio.wait_for(entered.wait(), timeout=1)
    await first.close()
    repository = SessionRepository(tmp_path / "sessions")
    assert repository.load(created.session_id).catalog.title_generation_turn == 0

    class ResumedResources(LocalResources):
        async def generate_session_title(self, input_text: str) -> str:
            assert input_text == "需要恢复的标题"
            return "已恢复的标题"

    second = WorkspaceHost(ResumedResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await second.open()
    try:
        async with asyncio.timeout(1):
            while (await second.dispatch(ListSessions())).sessions[0].title_pending:  # noqa: ASYNC110
                await asyncio.sleep(0)
        assert (await second.dispatch(ListSessions())).sessions[0].title == "已恢复的标题"
        assert len(repository.load(created.session_id).turns) == 1
    finally:
        await second.close()


async def test_workspace_host_sends_image_artifacts_without_persisting_base64(
    tmp_path: Path,
) -> None:
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"lumen-image-payload"
    received_images: list[list[BinaryContent]] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        call_images: list[BinaryContent] = []
        for request in messages:
            if not isinstance(request, ModelRequest):
                continue
            for user_part in request.parts:
                if not isinstance(user_part, UserPromptPart) or isinstance(user_part.content, str):
                    continue
                call_images.extend(
                    item for item in user_part.content if isinstance(item, BinaryContent)
                )
        received_images.append(call_images)
        yield "image received"

    artifact_store = ArtifactStore(tmp_path / "artifacts")
    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
        attachment_store=AttachmentStore(artifact_store),
    )
    resources = LocalResources(tmp_path, runtime, artifact_store=artifact_store)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        stored = await host.dispatch(
            StoreAttachment(
                filename="diagram.png",
                media_type="image/png",
                content=image_bytes,
            )
        )
        attachment = stored.data["attachment"]
        started = await host.dispatch(
            StartRun(
                created.session_id,
                "What is in this image?",
                "image-request",
                attachments=(attachment,),
            )
        )
        _ = [event async for event in host.subscribe(started.run_id)]
        follow_up = await host.dispatch(
            StartRun(created.session_id, "Use the same image context.", "image-follow-up")
        )
        _ = [event async for event in host.subscribe(follow_up.run_id)]
    finally:
        await host.close()

    assert [[image.data for image in call] for call in received_images] == [
        [image_bytes],
        [image_bytes],
    ]
    loaded = resources.session_repository.load(created.session_id)
    assert loaded.turns[0].attachments[0].artifact_ref == attachment["artifact_ref"]
    persisted = loaded.metadata.path.read_text(encoding="utf-8")
    assert base64.b64encode(image_bytes).decode() not in persisted
    assert attachment["artifact_ref"] in persisted


async def test_workspace_host_rejects_images_before_provider_io_when_model_is_text_only(
    tmp_path: Path,
) -> None:
    provider_called = False

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal provider_called
        provider_called = True
        yield "unexpected"

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    resources.active_model_config = lambda: SimpleNamespace(  # type: ignore[method-assign]
        id="text-only",
        input_modalities=("text",),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        stored = await host.dispatch(
            StoreAttachment(
                filename="diagram.png",
                media_type="image/png",
                content=b"\x89PNG\r\n\x1a\ntext-only-gate",
            )
        )
        with pytest.raises(InvalidStateError, match="does not declare image input support"):
            await host.dispatch(
                StartRun(
                    created.session_id,
                    "inspect",
                    "text-only-image",
                    attachments=(stored.data["attachment"],),
                )
            )
    finally:
        await host.close()

    assert provider_called is False
    assert resources.session_repository.load(created.session_id).turns == []


async def test_workspace_host_runs_and_replays_a_session_through_its_public_interface(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(
            StartRun(
                session_id=created.session_id,
                input="hello web",
                client_request_id="request-1",
            )
        )

        events = [event async for event in host.subscribe(started.run_id)]
        snapshot = await host.snapshot(created.session_id)
    finally:
        await host.close()

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.type for event in events] == [
        "run.started",
        "assistant.delta",
        "usage.updated",
        "run.completed",
    ]
    assert events[1].data == {"text": "web ready"}
    assert snapshot.session_id == created.session_id
    assert snapshot.last_user_input == "hello web"
    assert snapshot.active_run_id is None
    assert snapshot.timeline[-1].kind.value == "assistant"
    assert snapshot.timeline[-1].text == "web ready"


async def test_workspace_host_manages_session_catalog_and_hides_empty_sessions(
    tmp_path: Path,
) -> None:
    host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await host.open()
    try:
        empty = await host.dispatch(CreateSession())
        assert (await host.dispatch(ListSessions())).sessions == []

        started = await host.dispatch(StartRun(empty.session_id, "catalog task", "catalog-request"))
        _ = [event async for event in host.subscribe(started.run_id)]

        renamed = await host.dispatch(RenameSession(empty.session_id, "  Catalog   review  "))
        assert renamed.data == {"title": "Catalog review"}
        active = await host.dispatch(ListSessions())
        assert [(item.title, item.archived) for item in active.sessions] == [
            ("Catalog review", False)
        ]

        await host.dispatch(SetSessionArchived(empty.session_id, True))
        assert (await host.dispatch(ListSessions())).sessions == []
        archived = await host.dispatch(ListSessions(include_archived=True))
        assert [(item.title, item.archived) for item in archived.sessions] == [
            ("Catalog review", True)
        ]

        await host.dispatch(SetSessionArchived(empty.session_id, False))
        await host.dispatch(DeleteSession(empty.session_id))
        assert (await host.dispatch(ListSessions(include_archived=True))).sessions == []
        with pytest.raises(SessionNotFoundError):
            await host.snapshot(empty.session_id)
    finally:
        await host.close()


async def test_visibility_changes_preserve_unresolved_effects_and_invalidate_other_host_cache(
    tmp_path: Path,
) -> None:
    resources = LocalResources(tmp_path, _runtime())
    resources.task_workspace = TaskWorkspace(tmp_path, resources.artifact_store, resources.session_repository)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    other = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await host.open()
    await other.open()
    try:
        session = await host.dispatch(CreateSession())
        repo = resources.session_repository
        repo.append_turn_started(session.session_id, user_input="failed research", interaction_id="old-run")
        for i in range(6):
            repo.append_effect(session.session_id, EffectReceipt(
                id=f"effect:{i}", effect_kind=EffectKind.UNKNOWN, operation="exa_web_search_exa",
                status=EffectStatus.RECONCILIATION_REQUIRED,
            ))
        await other.snapshot(session.session_id)  # Populate a different Host's actor cache.
        before = repo.load(session.session_id)
        original_bytes = before.metadata.path.read_bytes()
        await host.dispatch(SetSessionArchived(session.session_id, True))
        assert repo.load(session.session_id).work_state == before.work_state
        await host.dispatch(SetSessionArchived(session.session_id, False))
        with pytest.raises(InvalidStateError, match="待核实"):
            await host.dispatch(StartRun(session.session_id, "retry", "retry"))
        assert (await host.dispatch(DeleteSession(session.session_id))).status == "deleted"
        after = repo.load(session.session_id)
        assert after.catalog.deleted_at is not None
        assert after.metadata.path.read_bytes().startswith(original_bytes)
        assert after.work_state == before.work_state
        assert after.turns == before.turns
        deleted_bytes = after.metadata.path.read_bytes()
        assert (await host.dispatch(DeleteSession(session.session_id))).status == "deleted"
        assert after.metadata.path.read_bytes() == deleted_bytes
        with pytest.raises(SessionNotFoundError):
            await other.snapshot(session.session_id)
        assert (await other.dispatch(ListSessions(include_archived=True))).sessions == []
    finally:
        await other.close()
        await host.close()


@pytest.mark.parametrize("action", ["archive", "delete"])
async def test_visibility_changes_do_not_release_another_execution_lease(
    tmp_path: Path, action: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    session = await host.dispatch(CreateSession())
    command = (
        DeleteSession(session.session_id)
        if action == "delete" else SetSessionArchived(session.session_id, True)
    )
    # Another Host / process owning the OS lock must prevent catalog mutation.
    owner = WorkspaceRunLock(tmp_path)
    assert owner.acquire()
    try:
        with pytest.raises(WorkspaceBusyError):
            await host.dispatch(command)
        assert owner.held
    finally:
        owner.release()
    owned_lock = WorkspaceRunLock(tmp_path)
    monkeypatch.setattr(host, "_workspace_run_lock", owned_lock)
    assert owned_lock.acquire()
    try:
        with pytest.raises(WorkspaceBusyError):
            await host.dispatch(command)
        assert owned_lock.held
        contender = WorkspaceRunLock(tmp_path)
        assert not contender.acquire()
    finally:
        owned_lock.release()
    await host.dispatch(command)


@pytest.mark.parametrize("status", [
    AgentStatus.RUNNING, AgentStatus.APPROVAL_PENDING, AgentStatus.IMPORT_PENDING,
])
async def test_visibility_gate_checks_agent_activity_instead_of_completion(
    tmp_path: Path, status: AgentStatus,
) -> None:
    resources = LocalResources(tmp_path, _runtime())
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    session = await host.dispatch(CreateSession())
    resources.session_repository.append_agent_thread(session.session_id, AgentThreadState(
        ref=AgentThreadRef(id="child", path="/root/child", parent_session_id=session.session_id,
                           root_run_id="older-run", agent_type="worker"),
        config=AgentConfigSnapshot(model_name="test", model_id="test", cwd=str(tmp_path)),
        status=status, task="work", task_name="child", idempotency_key="sha256:" + "0" * 64,
    ))
    if status is AgentStatus.IMPORT_PENDING:
        await host.dispatch(DeleteSession(session.session_id))
    else:
        with pytest.raises(InvalidStateError, match="execution is active"):
            await host.dispatch(DeleteSession(session.session_id))


async def test_workspace_host_persists_first_prompt_and_timeline_before_run_finishes(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    started_run_id: str | None = None
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(
            StartRun(created.session_id, "  investigate   refresh persistence  ", "refresh-request")
        )
        started_run_id = started.run_id
        await asyncio.wait_for(entered.wait(), timeout=1)

        summaries = await host.dispatch(ListSessions())
        reloaded = SessionRepository(tmp_path / "sessions").load(created.session_id)
        refreshed = await host.snapshot(created.session_id)

        cold_host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
        await cold_host.open()
        try:
            cold_snapshot = await cold_host.snapshot(created.session_id)
        finally:
            await cold_host.close()

        assert [(item.session_id, item.title) for item in summaries.sessions] == [
            (created.session_id, "investigate refresh persistence")
        ]
        assert reloaded.catalog.title == "investigate refresh persistence"
        assert [(turn.user_input, turn.status) for turn in reloaded.turns] == [
            ("  investigate   refresh persistence  ", "running")
        ]
        assert refreshed.active_run_id == started.run_id
        assert [(item.kind.value, item.text) for item in refreshed.timeline] == [
            ("user", "  investigate   refresh persistence  ")
        ]
        assert [(item.kind.value, item.text) for item in cold_snapshot.timeline] == [
            ("user", "  investigate   refresh persistence  "),
            ("system", "任务在完成前中断。你可以重新发送或重试此任务。"),
        ]
    finally:
        release.set()
        if started_run_id is not None:
            _ = [event async for event in host.subscribe(started_run_id)]
        await host.close()


async def test_workspace_host_allows_second_host_reads_but_rejects_concurrent_execution(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    second_history: list[list[ModelMessage]] = []

    async def first_stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        yield "first complete"

    async def second_stream(
        messages: list[ModelMessage], _info: AgentInfo
    ):  # type: ignore[no-untyped-def]
        second_history.append(messages)
        yield "second complete"

    first_resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=first_stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    second_resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=second_stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    first = WorkspaceHost(first_resources)  # type: ignore[arg-type]
    second = WorkspaceHost(second_resources)  # type: ignore[arg-type]
    await first.open()
    await second.open()
    first_run_id: str | None = None
    try:
        created = await first.dispatch(CreateSession())
        started = await first.dispatch(StartRun(created.session_id, "first", "first-request"))
        first_run_id = started.run_id
        await asyncio.wait_for(entered.wait(), timeout=1)

        read_only = await second.snapshot(created.session_id)
        assert read_only.active_run_id is None
        assert read_only.timeline[-1].status == "interrupted"
        with pytest.raises(WorkspaceBusyError, match="another Lumen process"):
            await second.dispatch(StartRun(created.session_id, "compete", "competing-request"))

        release.set()
        _ = [event async for event in first.subscribe(started.run_id)]
        continued = await second.dispatch(
            StartRun(created.session_id, "continue", "second-request")
        )
        _ = [event async for event in second.subscribe(continued.run_id)]
    finally:
        release.set()
        if first_run_id is not None:
            _ = [event async for event in first.subscribe(first_run_id)]
        await second.close()
        await first.close()

    assert second_history
    assert "first complete" in str(second_history[0])
    loaded = SessionRepository(tmp_path / "sessions").load(created.session_id)
    assert [turn.status for turn in loaded.turns] == ["completed", "completed"]


async def test_workspace_host_retry_restores_interrupted_turn_attachments(tmp_path: Path) -> None:
    image_bytes = b"\x89PNG\r\n\x1a\ninterrupted-attachment"
    received: list[bytes] = []

    async def stream(
        messages: list[ModelMessage], _info: AgentInfo
    ):  # type: ignore[no-untyped-def]
        for message in messages:
            if not isinstance(message, ModelRequest):
                continue
            for part in message.parts:
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str):
                    received.extend(
                        item.data for item in part.content if isinstance(item, BinaryContent)
                    )
        yield "recovered"

    artifact_store = ArtifactStore(tmp_path / "artifacts")
    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
            attachment_store=AttachmentStore(artifact_store),
        ),
        artifact_store=artifact_store,
    )
    session = resources.session_repository.create(agent_name="test-agent", model_id="test-model")
    attachment = AttachmentStore(artifact_store).store_image(
        filename="interrupted.png",
        media_type="image/png",
        content=image_bytes,
    )
    resources.session_repository.append_turn_started(
        session.id,
        user_input="inspect the interrupted image",
        interaction_id="orphaned-run",
        attachments=(attachment,),
    )

    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        snapshot = await host.snapshot(session.id)
        retried = await host.dispatch(RetryRun(session.id, "retry-interrupted"))
        _ = [event async for event in host.subscribe(retried.run_id)]
    finally:
        await host.close()

    assert snapshot.timeline[-1].status == "interrupted"
    assert received == [image_bytes]
    loaded = resources.session_repository.load(session.id)
    assert [turn.status for turn in loaded.turns] == ["running", "completed"]
    assert loaded.turns[-1].attachments == [attachment]


async def test_workspace_host_blocks_interrupted_retry_with_unresolved_unknown_effect(
    tmp_path: Path,
) -> None:
    provider_called = False

    async def stream(
        _messages: list[ModelMessage], _info: AgentInfo
    ):  # type: ignore[no-untyped-def]
        nonlocal provider_called
        provider_called = True
        yield "unsafe duplicate"

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    resources.task_workspace = TaskWorkspace(
        tmp_path,
        resources.artifact_store,
        resources.session_repository,
    )
    session = resources.session_repository.create(agent_name="test-agent", model_id="test-model")
    resources.task_workspace.bind_session(session.id)
    resources.session_repository.append_turn_started(
        session.id,
        user_input="repeat an unknown remote action",
        interaction_id="orphaned-unknown-effect",
    )
    effect = resources.task_workspace.record_tool_effect(
        tool_name="remote_action",
        effect_kind=EffectKind.UNKNOWN,
        success=True,
        summary="remote endpoint returned success before the process stopped",
    )
    assert effect is not None
    assert effect.status is EffectStatus.RECONCILIATION_REQUIRED

    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        with pytest.raises(InvalidStateError, match="resolve interrupted run effects") as captured:
            await host.dispatch(RetryRun(session.id, "retry-unknown-effect"))
    finally:
        await host.close()

    assert provider_called is False
    assert effect.id in str(captured.value.details["issues"])


async def test_workspace_host_persists_terminal_failure_when_completed_turn_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = LocalResources(tmp_path, _runtime("durable answer"))
    original_append_turn = resources.session_repository.append_turn

    def append_turn_with_terminal_failure(*args: object, **kwargs: object) -> None:
        if kwargs.get("status") == "completed":
            raise TypeError("provider message could not be serialized")
        original_append_turn(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(resources.session_repository, "append_turn", append_turn_with_terminal_failure)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(
            StartRun(created.session_id, "keep this conversation", "terminal-write-failure")
        )
        _ = [event async for event in host.subscribe(started.run_id)]

        loaded = SessionRepository(tmp_path / "sessions").load(created.session_id)
        snapshot = await host.snapshot(created.session_id)

        assert [(turn.user_input, turn.status) for turn in loaded.turns] == [
            ("keep this conversation", "failed")
        ]
        assert snapshot.active_run_id is None
        assert [item.kind.value for item in snapshot.timeline] == ["user", "assistant", "error"]
        assert snapshot.timeline[-2].text == "durable answer"
        assert "provider message could not be serialized" in snapshot.timeline[-1].text
    finally:
        await host.close()


async def test_workspace_host_persists_decimal_enriched_usage_and_cold_reloads_answer(
    tmp_path: Path,
) -> None:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "durable decimal answer"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
        usage_enricher=lambda _session_id, usage: {
            **usage,
            "cost": Decimal("0.0026551616"),
        },
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    await host.open()
    try:
        created = await host.dispatch(CreateSession())
        started = await host.dispatch(
            StartRun(created.session_id, "decimal provider usage", "decimal-provider-usage")
        )
        _ = [event async for event in host.subscribe(started.run_id)]
    finally:
        await host.close()

    loaded = SessionRepository(tmp_path / "sessions").load(created.session_id)
    cold_host = WorkspaceHost(LocalResources(tmp_path, _runtime()))  # type: ignore[arg-type]
    await cold_host.open()
    try:
        snapshot = await cold_host.snapshot(created.session_id)
    finally:
        await cold_host.close()

    assert loaded.turns[0].status == "completed"
    assert loaded.turns[0].usage["cost"] == "0.0026551616"
    assert [item.kind.value for item in snapshot.timeline] == ["user", "assistant"]
    assert snapshot.timeline[-1].text == "durable decimal answer"


async def test_workspace_host_deduplicates_requests_and_rejects_parallel_runs(
    tmp_path: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        yield "done"

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        first_session = await host.dispatch(CreateSession())
        second_session = await host.dispatch(CreateSession())
        command = StartRun(first_session.session_id, "one", "same-request")
        first = await host.dispatch(command)
        await asyncio.wait_for(entered.wait(), timeout=1)

        duplicate = await host.dispatch(command)
        with pytest.raises(WorkspaceBusyError):
            await host.dispatch(StartRun(second_session.session_id, "two", "request-2"))

        release.set()
        _ = [event async for event in host.subscribe(first.run_id)]
    finally:
        await host.close()

    assert duplicate.run_id == first.run_id


async def test_plan_review_is_revisioned_persistent_and_idempotent(tmp_path: Path) -> None:
    phase = 0

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal phase
        if phase == 0:
            phase = 1
            yield {
                0: DeltaToolCall(
                    "set_plan",
                    '{"goal":"Ship safely","steps":[{"id":"implement","title":"Implement"}]}',
                    tool_call_id="plan-create",
                )
            }
        elif phase == 1:
            phase = 2
            yield "Plan ready."
        elif phase == 2:
            phase = 3
            yield {
                0: DeltaToolCall(
                    "update_step",
                    '{"step_id":"implement","status":"in_progress"}',
                    tool_call_id="step-start",
                )
            }
        elif phase == 3:
            phase = 4
            yield {
                0: DeltaToolCall(
                    "update_step",
                    '{"step_id":"implement","status":"completed"}',
                    tool_call_id="step-finish",
                )
            }
        else:
            yield "Approved revision executed."

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        await host.dispatch(SetCollaborationMode(session.session_id, "plan"))
        planning = await host.dispatch(
            StartRun(session.session_id, "plan this", "planning-request")
        )
        planning_events = [event async for event in host.subscribe(planning.run_id)]
        pending = await host.snapshot(session.session_id)
        approved = await host.dispatch(
            ApprovePlan(session.session_id, pending.plan["revision"], "approve-request")
        )
        duplicate = await host.dispatch(
            ApprovePlan(session.session_id, pending.plan["revision"], "approve-request")
        )
        execution_events = [event async for event in host.subscribe(approved.run_id)]
        restored = resources.session_repository.load(session.session_id)
    finally:
        await host.close()

    assert "plan.review_pending" in [event.type for event in planning_events]
    assert pending.plan_review_status == "review_pending"
    assert pending.collaboration_mode == "plan"
    assert duplicate.run_id == approved.run_id
    assert execution_events[0].type == "plan.review_resolved"
    assert execution_events[-1].type == "run.completed"
    assert restored.plan.approved_revision == restored.plan.revision


async def test_workspace_host_cancel_is_persisted_and_idempotent(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        entered.set()
        await asyncio.Event().wait()
        yield "unreachable"

    resources = LocalResources(
        tmp_path,
        AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={},
        ),
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "wait", "cancel-request"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        cancelled = await host.dispatch(CancelRun(started.run_id))
        again = await host.dispatch(CancelRun(started.run_id))
        events = [event async for event in host.subscribe(started.run_id)]
        turn = resources.session_repository.load(session.session_id).turns[0]
    finally:
        await host.close()

    assert cancelled.status == "cancelled"
    assert again.status == "cancelled"
    assert events[-1].type == "run.cancelled"
    assert turn.status == "cancelled"


@pytest.mark.parametrize("mode", ["default", "plan"])
async def test_edited_message_uses_only_prefix_and_new_prompt(
    tmp_path: Path, mode: Literal["default", "plan"],
) -> None:
    received: list[list[str]] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        received.append([
            part.content for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ])
        if mode == "plan" and not any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            yield {0: DeltaToolCall(
                "set_plan", '{"goal":"Draft the updated request","steps":[{"id":"one","title":"Implement"}]}',
                tool_call_id=f"plan-{len(received)}",
            )}
            return
        yield "reply"

    resources = LocalResources(tmp_path, AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[], toolsets=[], instructions="help",
        limits=LimitsConfig(), tool_metadata={},
    ))
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        await host.dispatch(SetCollaborationMode(session.session_id, mode))
        for index, prompt in enumerate(["prefix", "old question", "old follow-up"]):
            started = await host.dispatch(StartRun(session.session_id, prompt, f"source-{index}"))
            assert [event async for event in host.subscribe(started.run_id)][-1].type == "run.completed"
        source = resources.session_repository.load(session.session_id)
        before = source.metadata.path.read_bytes()
        command = ForkSessionAtTurn(session.session_id, 1, include_turn=False, client_request_id="edit-1")
        forked = await host.dispatch(command)
        assert await host.dispatch(command) == forked
        with pytest.raises(InvalidStateError, match="different parameters"):
            await host.dispatch(ForkSessionAtTurn(
                session.session_id, 0, include_turn=False, client_request_id="edit-1",
            ))
        started = await host.dispatch(StartRun(forked.session_id, "replacement", "replacement-run"))
        assert await host.dispatch(StartRun(forked.session_id, "replacement", "replacement-run")) == started
        assert [event async for event in host.subscribe(started.run_id)][-1].type == "run.completed"
        assert [prompt.rsplit("\n\n", 1)[-1] for prompt in received[-1]] == ["prefix", "replacement"]
        branch = resources.session_repository.load(forked.session_id)
        assert [turn.user_input for turn in branch.turns] == ["prefix", "replacement"]
        assert str(branch.settings.collaboration_mode) == mode
        assert source.metadata.path.read_bytes() == before
    finally:
        await host.close()


@pytest.mark.parametrize("mode", ["default", "plan"])
async def test_edit_and_start_preserve_source_until_external_results_are_resolved(
    tmp_path: Path, mode: Literal["default", "plan"],
) -> None:
    resources = LocalResources(tmp_path, _runtime())
    resources.task_workspace = TaskWorkspace(tmp_path, resources.artifact_store, resources.session_repository)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        await host.dispatch(SetCollaborationMode(session.session_id, mode))
        resources.session_repository.append_turn_started(
            session.session_id, user_input="old prompt", interaction_id="old-run",
        )
        resources.task_workspace.bind_session(session.session_id)
        effect = resources.task_workspace.record_tool_effect(
            tool_name="remote", effect_kind=EffectKind.UNKNOWN, success=True, summary="old outcome",
        )
        assert effect is not None
        count = len(list(resources.session_repository.directory.glob("*.jsonl")))
        before = resources.session_repository.load(session.session_id).metadata.path.read_bytes()
        with pytest.raises(InvalidStateError, match="待核实"):
            await host.dispatch(ForkSessionAtTurn(session.session_id, 0, include_turn=False))
        with pytest.raises(InvalidStateError, match="待核实"):
            await host.dispatch(StartRun(session.session_id, "replacement", "replace"))
        assert len(list(resources.session_repository.directory.glob("*.jsonl"))) == count
        assert resources.session_repository.load(session.session_id).metadata.path.read_bytes() == before
        await host.dispatch(WaivePlanVerification(session.session_id, (effect.id,), "result checked by user"))
        forked = await host.dispatch(ForkSessionAtTurn(session.session_id, 0, include_turn=False))
        assert forked.session_id != session.session_id
    finally:
        await host.close()


@pytest.mark.parametrize("mode", ["default", "plan"])
async def test_regenerate_reuses_session_identity_and_excludes_abandoned_suffix(
    tmp_path: Path, mode: Literal["default", "plan"],
) -> None:
    received: list[list[str]] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        received.append([
            part.content for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ])
        if mode == "plan" and not any(
            isinstance(part, ToolReturnPart) for part in messages[-1].parts
        ):
            yield {0: DeltaToolCall(
                "set_plan",
                '{"goal":"Regenerate","steps":[{"id":"one","title":"Answer"}]}',
                tool_call_id=f"plan-{len(received)}",
            )}
            return
        yield "fresh answer"

    resources = LocalResources(tmp_path, AgentRuntime(
        model=FunctionModel(stream_function=stream), tools=[], toolsets=[], instructions="help",
        limits=LimitsConfig(), tool_metadata={},
    ))
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        await host.dispatch(SetCollaborationMode(session.session_id, mode))
        for index, prompt in enumerate(["prefix", "same prompt", "stale follow-up"]):
            started = await host.dispatch(StartRun(session.session_id, prompt, f"old-{index}"))
            assert [event async for event in host.subscribe(started.run_id)][-1].type == "run.completed"
        await host.dispatch(RenameSession(session.session_id, "Stable title"))

        started = await host.dispatch(StartRun(
            session.session_id,
            "same prompt",
            "regenerate-same-text",
            regenerate_from_turn=1,
        ))
        assert started.session_id == session.session_id
        assert [event async for event in host.subscribe(started.run_id)][-1].type == "run.completed"

        loaded = resources.session_repository.load(session.session_id)
        resources.session_repository.clear_projection_cache()
        cold = resources.session_repository.load(session.session_id)
        assert loaded.catalog.title == cold.catalog.title == "Stable title"
        assert [turn.user_input for turn in cold.turns] == ["prefix", "same prompt"]
        assert [prompt.rsplit("\n\n", 1)[-1] for prompt in received[-1]] == [
            "prefix",
            "same prompt",
        ]
        assert all("stale follow-up" not in str(message) for message in cold.full_history)
        assert len(list(resources.session_repository.directory.glob("*.jsonl"))) == 1
    finally:
        await host.close()


async def test_host_creates_scoped_user_waiver_receipt(tmp_path: Path) -> None:
    resources = LocalResources(tmp_path, _runtime())
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        plan = PlanState(
            revision=2,
            approved_revision=2,
            steps=[PlanStep(id="done", title="Done", status=StepStatus.COMPLETED)],
            evidence=[
                EvidenceReceipt(
                    id="ediff",
                    kind=EvidenceKind.DIFF,
                    source_id="write-call",
                    summary="file changed",
                    passed=True,
                    sequence=1,
                )
            ],
        )
        actor = host._actor(session.session_id)  # type: ignore[reportPrivateUsage]
        actor.coordinator.replace_plan(plan)
        resources.session_repository.append_plan_state(session.session_id, plan)

        result = await host.dispatch(
            WaivePlanVerification(session.session_id, (), "No project test command exists")
        )
        restored = resources.session_repository.load(session.session_id).plan
    finally:
        await host.close()

    assert result.status == "waived"
    assert restored.evidence[-1].kind is EvidenceKind.USER_WAIVER
    assert restored.evidence[-1].sequence > restored.evidence[0].sequence


async def test_host_waives_a_work_effect_without_an_approved_plan(tmp_path: Path) -> None:
    resources = LocalResources(tmp_path, _runtime())
    resources.task_workspace = TaskWorkspace(
        tmp_path,
        ArtifactStore(tmp_path / "artifacts"),
        resources.session_repository,
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        resources.task_workspace.bind_session(session.session_id)
        receipt = resources.task_workspace.record_tool_effect(
            tool_name="remote_action",
            effect_kind=EffectKind.UNKNOWN,
            success=True,
            summary="remote tool returned success",
        )
        assert receipt is not None
        before = await host.snapshot(session.session_id)

        result = await host.dispatch(
            WaivePlanVerification(session.session_id, (receipt.id,), "confirmed remotely")
        )
        after = await host.snapshot(session.session_id)
    finally:
        await host.close()

    assert len(before.pending_effects) == 1
    assert result.status == "waived"
    assert result.data["effect_count"] == 1
    assert after.pending_effects == []
    state = resources.session_repository.load(session.session_id).work_state
    assert state.effects[-1].status is EffectStatus.VERIFIED


async def test_host_snapshot_exposes_current_and_recoverable_work_product_state(
    tmp_path: Path,
) -> None:
    resources = LocalResources(tmp_path, _runtime())
    resources.task_workspace = TaskWorkspace(
        tmp_path,
        ArtifactStore(tmp_path / "artifacts"),
        resources.session_repository,
    )
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        (tmp_path / "report.md").write_text("before", encoding="utf-8")
        resources.task_workspace.bind_session(session.session_id)
        opened = resources.task_workspace.open_work_product("report.md")
        resources.task_workspace.change_work_product(opened["id"], "whole", "after")

        snapshot = await host.snapshot(session.session_id)
    finally:
        await host.close()

    assert snapshot.work_products[0]["resource"] == "report.md"
    assert snapshot.pending_effects == []
    assert snapshot.recoverable_effects[0]["status"] == "verified"


def _last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


async def test_workspace_host_manual_approval_can_be_resolved_from_another_request(
    tmp_path: Path,
) -> None:
    executed = False

    def write_note(content: str) -> str:
        nonlocal executed
        executed = True
        return content

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        result = _last_tool_return(messages)
        if result is None:
            yield {0: DeltaToolCall("write_note", '{"content":"safe"}', tool_call_id="call-web")}
        else:
            yield f"handled:{result.outcome}"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "plugin:test", "risk": "write"}},
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "write", "approval-request"))
        stream_events = host.subscribe(started.run_id)
        seen: list[EventEnvelope] = []
        async for event in stream_events:
            seen.append(event)
            if event.type == "approval.pending":
                assert event.data["presentation"]["title"] == "write_note · write · plugin:test"
                await host.dispatch(DecideApproval(started.run_id, "call-web", False))
                repeated = await host.dispatch(DecideApproval(started.run_id, "call-web", False))
                assert repeated.status == "denied"
                with pytest.raises(ApprovalAlreadyResolvedError):
                    await host.dispatch(DecideApproval(started.run_id, "call-web", True))
        turn = host.resources.session_repository.load(session.session_id).turns[0]
    finally:
        await host.close()

    assert executed is False
    assert any(event.type == "approval.resolved" and not event.data["approved"] for event in seen)
    assert turn.approvals[0]["approved"] is False


async def test_workspace_host_remembers_bounded_approval_for_the_session(
    tmp_path: Path,
) -> None:
    model_step = 0
    executions: list[str] = []

    def write_note(content: str) -> str:
        executions.append(content)
        return content

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal model_step
        model_step += 1
        if model_step <= 2:
            yield {
                0: DeltaToolCall(
                    "write_note",
                    f'{{"content":"note-{model_step}"}}',
                    tool_call_id=f"call-{model_step}",
                )
            }
        else:
            yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={"write_note": {"origin": "plugin:test", "risk": "write"}},
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(StartRun(session.session_id, "write twice", "remember"))
        pending_calls: list[str] = []
        async for event in host.subscribe(started.run_id):
            if event.type == "approval.pending":
                pending_calls.append(str(event.data["call_id"]))
                await host.dispatch(
                    DecideApproval(started.run_id, str(event.data["call_id"]), True, "session")
                )
    finally:
        await host.close()

    assert pending_calls == ["call-1"]
    assert executions == ["note-1", "note-2"]


async def test_workspace_host_persists_always_rules_across_hosts(tmp_path: Path) -> None:
    """scope="always" survives host restarts via the project rule store."""

    model_step = 0
    executions: list[str] = []

    def write_note(content: str) -> str:
        executions.append(content)
        return content

    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal model_step
        model_step += 1
        if model_step <= 2:
            yield {
                0: DeltaToolCall(
                    "write_note",
                    f'{{"content":"note-{model_step}"}}',
                    tool_call_id=f"call-{model_step}",
                )
            }
        else:
            yield "done"

    def build_runtime() -> AgentRuntime:
        return AgentRuntime(
            model=FunctionModel(stream_function=stream),
            tools=[Tool(write_note, sequential=True, requires_approval=True)],
            toolsets=[],
            instructions="help",
            limits=LimitsConfig(),
            tool_metadata={"write_note": {"origin": "plugin:test", "risk": "write"}},
        )

    async def run_once() -> list[str]:
        nonlocal model_step
        model_step = 0
        host = WorkspaceHost(LocalResources(tmp_path, build_runtime()))  # type: ignore[arg-type]
        await host.open()
        try:
            session = await host.dispatch(CreateSession())
            started = await host.dispatch(StartRun(session.session_id, "write twice", "remember"))
            pending_calls: list[str] = []
            async for event in host.subscribe(started.run_id):
                if event.type == "approval.pending":
                    pending_calls.append(str(event.data["call_id"]))
                    await host.dispatch(
                        DecideApproval(started.run_id, str(event.data["call_id"]), True, "always")
                    )
            return pending_calls
        finally:
            await host.close()

    assert await run_once() == ["call-1"]
    # A brand-new host over the same workspace reloads the persisted rule and
    # auto-approves both calls without prompting.
    assert await run_once() == []
    assert executions == ["note-1", "note-2", "note-1", "note-2"]


async def test_workspace_host_expands_file_mentions_for_every_client_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.md").write_text("shared host context", encoding="utf-8")
    seen: list[str] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        seen.append(str(messages[-1]))
        yield "done"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        started = await host.dispatch(
            StartRun(session.session_id, "review @context.md", "web-style-input")
        )
        async for _event in host.subscribe(started.run_id):
            pass
    finally:
        await host.close()

    assert seen
    assert "shared host context" in seen[0]
    assert '<file path="context.md">' in seen[0]


async def test_workspace_host_projects_agent_state_for_all_clients(tmp_path: Path) -> None:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield "unused"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )
    host = WorkspaceHost(LocalResources(tmp_path, runtime))  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        host.resources.session_repository.append_agent_thread(
            session.session_id,
            AgentThreadState(
                ref=AgentThreadRef(
                    id="agent-one",
                    path="/root/explorer",
                    parent_session_id=session.session_id,
                    root_run_id="run-one",
                    agent_type="explorer",
                ),
                task="inspect the parser",
                task_name="explorer",
                config=AgentConfigSnapshot(
                    model_name="test",
                    model_id="test-model",
                    cwd=str(tmp_path),
                ),
                idempotency_key="sha256:" + "0" * 64,
            ),
        )
        snapshot = await host.snapshot(session.session_id)
    finally:
        await host.close()

    assert len(snapshot.agents) == 1
    agent = snapshot.agents[0]
    assert agent["id"] == "agent-one"
    assert agent["path"] == "/root/explorer"
    assert agent["role"] == "explorer"
    assert agent["status"] == "queued"
    assert agent["task"] == "inspect the parser"


async def test_workspace_host_invokes_skills_and_routes_context_controls(tmp_path: Path) -> None:
    seen_prompts: list[str] = []

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        seen_prompts.append(str(messages[-1]))
        yield "skill complete"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=[],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata={},
    )

    class FakeContextEngine:
        async def control(self, command: object, emit: object) -> object:
            del emit
            return SimpleNamespace(
                status="ok",
                message=type(command).__name__,
                payload={"action": getattr(command, "action", "report")},
            )

    runtime.context_engine = FakeContextEngine()  # type: ignore[assignment]
    resources = LocalResources(tmp_path, runtime)
    skill_file = tmp_path / ".lumen" / "skills" / "review" / "SKILL.md"
    resources.skills = [
        Skill(
            name="review",
            description="Review code",
            file_path=skill_file,
            base_dir=skill_file.parent,
            body="Inspect the selected file carefully.",
            source="project",
        )
    ]
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        session = await host.dispatch(CreateSession())
        report = await host.dispatch(ContextControl(session.session_id, "report"))
        memory = await host.dispatch(ContextControl(session.session_id, "memory", action="list"))
        runtime.context_engine = None
        started = await host.dispatch(
            InvokeSkill(session.session_id, "review", "src/main.py", "skill-request")
        )
        _ = [event async for event in host.subscribe(started.run_id)]
        turn = resources.session_repository.load(session.session_id).turns[-1]
    finally:
        await host.close()

    assert report.data["message"] == "ContextReportCommand"
    assert memory.data["payload"] == {"action": "list"}
    assert turn.user_input == "/skill:review src/main.py"
    assert "Inspect the selected file carefully." in seen_prompts[-1]
    assert "src/main.py" in seen_prompts[-1]
