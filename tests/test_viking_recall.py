from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from lumen.config import ContextConfig, LimitsConfig
from lumen.context import AgentRef, ContextEngine, ContextRequest, RuntimeContextSnapshot, SessionRef
from lumen.context.engine import ContextEnvelope, TaskSnapshot
from lumen.context.memory import MemoryManager, MemoryRecord, MemoryStatus, SQLiteMemoryRepository, projection
from lumen.context.memory.projection import select_memory_index
from lumen.plan import PlanState
from lumen.runtime import AgentRuntime, ToolApproval

if TYPE_CHECKING:
    from lumen.context.memory.openviking import OpenVikingService


class SemanticFixture:
    def __init__(self, repository: SQLiteMemoryRepository, record_id: str) -> None:
        self.repository = repository
        self.record_id = record_id
        self.calls: list[tuple[str, set[str]]] = []

    async def find(self, query: str, exclude_ids: set[str]) -> tuple[str, ...]:
        self.calls.append((query, exclude_ids))
        return ("viking://fixture",)

    def resolve(self, uris: tuple[str, ...]) -> list[MemoryRecord]:
        assert uris == ("viking://fixture",)
        record = self.repository.get(self.record_id)
        return [record] if record and record.status is MemoryStatus.ACTIVE else []

    def set_incognito(self, enabled: bool) -> None:
        pass

    async def close(self) -> None:
        pass


async def _discard(_event: object) -> None:
    pass


def _request() -> ContextRequest:
    return ContextRequest(
        session=SessionRef(id="test"), agent=AgentRef(name="agent"),
        prompt="expanded private attachment", memory_query="who should receive the rollout?",
        task=TaskSnapshot(plan=PlanState()), runtime=RuntimeContextSnapshot(instructions="help"), history=(),
    )


async def test_complete_index_ids_follow_token_and_byte_limits(tmp_path: Path) -> None:
    manager = MemoryManager(SQLiteMemoryRepository(tmp_path / "memory.db"), project_id="p")
    long = manager.remember("alpha\nbeta\n" * 32)
    short = manager.remember("short fact")
    rendered, ids = select_memory_index([long, short], lambda text: len(text) < 100)
    assert ids == {short.id}
    assert long.id not in rendered and short.content in rendered
    await manager.close()


async def test_prepare_and_freeze_validate_uris_and_count_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(projection, "MAX_INDEX_LINES", 4)
    repository = SQLiteMemoryRepository(tmp_path / "memory.db")
    manager = MemoryManager(repository, project_id="p")
    first = manager.remember("always format with ruff")
    target = manager.remember("internal testers receive releases first")
    service = SemanticFixture(repository, target.id)
    manager.semantic = cast("OpenVikingService", service)
    engine = ContextEngine(ContextConfig(), model=TestModel(), memory=manager)
    request = _request()
    try:
        initial = await engine.prepare(request, _discard)
        assert service.calls == [(request.memory_query, {first.id})]
        assert target.content in str(initial.provider_history)
        assert target.content not in str(initial.canonical_history)
        user_message = ModelRequest(parts=[UserPromptPart(content=request.prompt)])
        envelope, messages = await engine.prepare_step(
            initial, [*initial.provider_history, user_message], [user_message], session_id="test",
            model_step=1, instructions="help", tool_schemas=[], output_reserve_tokens=200,
            task=request.task, emit=_discard,
        )
        record = repository.get(target.id)
        assert record and record.use_count == 0
        await engine.record_memory_use("test", envelope)
        await engine.record_memory_use("test", envelope)
        record = repository.get(target.id)
        assert record and record.use_count == 1
        manager.forget(target.id)
        _, messages = await engine.prepare_step(
            envelope, messages, [user_message], session_id="test",
            model_step=2, instructions="help", tool_schemas=[], output_reserve_tokens=200,
            task=request.task, emit=_discard,
        )
        assert target.content not in str(messages) and user_message in messages
        assert len(service.calls) == 1
    finally:
        await manager.close()


@pytest.mark.parametrize("query", [None, "", "please email demo@example.org"])
async def test_opaque_or_sensitive_input_stays_local(tmp_path: Path, query: str | None) -> None:
    repository = SQLiteMemoryRepository(tmp_path / "memory.db")
    service = SemanticFixture(repository, "absent")
    manager = MemoryManager(repository, project_id="p", semantic=cast("OpenVikingService", service))
    engine = ContextEngine(ContextConfig(), model=TestModel(), memory=manager)
    try:
        await engine.prepare(replace(_request(), memory_query=query), _discard)
        assert not service.calls
    finally:
        await manager.close()


async def test_provider_retry_refreezes_after_user_forgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoryManager(SQLiteMemoryRepository(tmp_path / "memory.db"), project_id="p")
    record = manager.remember("internal testers receive releases first")
    withdrawn = manager.remember("approval required from release team")
    checked: list[str] = []
    check_memory = ContextEngine.memory_is_current

    async def forget_during_preflight(
        engine: ContextEngine, envelope: ContextEnvelope, session_id: str,
    ) -> bool:
        if not checked:
            manager.forget(withdrawn.id)
        checked.append(session_id)
        return await check_memory(engine, envelope, session_id)

    monkeypatch.setattr(ContextEngine, "memory_is_current", forget_during_preflight)
    sent: list[bool] = []

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        assert withdrawn.content not in str(messages)
        sent.append(record.content in str(messages))
        if len(sent) == 1:
            manager.forget(record.id)
            raise ConnectionError("offline fixture")
        yield "ok"

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("unexpected approval")

    model = FunctionModel(stream_function=respond)
    engine = ContextEngine(ContextConfig(), model=model, memory=manager)
    runtime = AgentRuntime(
        model=model, tools=[], toolsets=[], instructions="answer", context_engine=engine,
        tool_metadata={}, limits=LimitsConfig(model_retry_delay_seconds=0),
    )
    try:
        outcome = await runtime.run("question", [], _discard, approve, session_id="test")
        assert outcome.output == "ok"
        assert sent == [True, False]
        assert checked == ["test", "test"]
        assert len(outcome.request_receipts) == 2
    finally:
        await manager.close()


@pytest.mark.parametrize("allow", [False, True])
async def test_dynamic_usage_counts_dispatched_turns_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, allow: bool,
) -> None:
    monkeypatch.setattr(projection, "MAX_INDEX_LINES", 0)
    repository = SQLiteMemoryRepository(tmp_path / "memory.db")
    manager = MemoryManager(repository, project_id="p")
    target = manager.remember("rollout starts on Monday")
    attempts = 0
    check_memory = ContextEngine.memory_is_current

    async def check_before_send(engine: ContextEngine, envelope: ContextEnvelope, session_id: str) -> bool:
        if not attempts:
            record = repository.get(target.id)
            assert record and record.use_count == 0
            if not allow:
                raise RuntimeError("stop before sending")
        return await check_memory(engine, envelope, session_id)

    monkeypatch.setattr(ContextEngine, "memory_is_current", check_before_send)

    async def respond(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        nonlocal attempts
        assert target.content in str(messages)
        attempts += 1
        if attempts == 1:
            raise ConnectionError("retry fixture")
        yield "ok"

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("unexpected approval")

    model = FunctionModel(stream_function=respond)
    engine = ContextEngine(ContextConfig(), model=model, memory=manager)
    runtime = AgentRuntime(
        model=model, tools=[], toolsets=[], instructions="answer", context_engine=engine,
        tool_metadata={}, limits=LimitsConfig(model_retry_delay_seconds=0),
    )
    try:
        if allow:
            outcome = await runtime.run("rollout", [], _discard, approve, session_id="test")
            assert outcome.output == "ok" and attempts == 2
        else:
            with pytest.raises(RuntimeError, match="stop before sending"):
                await runtime.run("rollout", [], _discard, approve, session_id="test")
            assert attempts == 0
        record = repository.get(target.id)
        assert record and record.use_count == int(allow)
    finally:
        await manager.close()
