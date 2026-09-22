"""Tests for the model-side artifact full-text search tool.

Receipts keep only head/tail excerpts of bulky tool outputs;
``search_artifacts`` finds the needle across every spilled body in the shared
store and hands back a ``char_start`` the model feeds straight into
``read_artifact`` to resume reading.
"""

# The unit tests deliberately exercise the private tool implementation and
# swap the store to a tmp path, matching test_read_artifact.py's style.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import LimitsConfig, load_config
from lumen.context.artifacts import ArtifactStore
from lumen.events import RunEvent
from lumen.resources import ResourceManager
from lumen.runtime import AgentRuntime, ToolApproval


def _manager(tmp_path: Path) -> ResourceManager:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model:
    id: test
tools:
  builtins: []
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)
    # Keep test artifacts out of the real ~/.lumen/artifacts store.
    manager._artifact_store = ArtifactStore(tmp_path / "artifacts")
    return manager


def test_search_artifacts_registration(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert "search_artifacts" in {tool.name for tool in manager.local_tools}
    assert manager.tool_metadata["search_artifacts"] == {
        "origin": "builtin:artifacts",
        "risk": "read",
        "effect": "observe",
    }


def test_search_artifacts_hits_and_hints(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    body = (
        "intro filler line\n" * 100
        + "ERROR: disk quota exceeded on /dev/sda1\n"
        + "outro filler line\n" * 100
    )
    ref = manager._artifact_store.store(body)

    result = manager._search_artifacts("quota exceeded")
    assert [hit.ref for hit in result.hits] == [ref]
    assert "**quota**" in result.hits[0].snippet
    assert "read_artifact" in result.hint

    empty = manager._search_artifacts("zzq nowhere present")
    assert empty.hits == []
    assert "未命中" in empty.hint


def test_search_artifacts_rejects_out_of_range_max_results(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="max_results must be between 1 and 20"):
        manager._search_artifacts("q", max_results=0)
    with pytest.raises(ValueError, match="max_results must be between 1 and 20"):
        manager._search_artifacts("q", max_results=21)


async def test_model_searches_then_reads_artifact(tmp_path: Path) -> None:
    """End-to-end: the model searches, then resumes reading at char_start."""
    manager = _manager(tmp_path)
    body = (
        "prefix context line\n" * 200
        + "MIDDLE_NEEDLE: rotation keys runbook\n"
        + "suffix context line\n" * 200
    )
    ref = manager._artifact_store.store(body)

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        returns = [
            (getattr(part, "tool_name", ""), getattr(part, "content", ""))
            for message in messages
            for part in getattr(message, "parts", [])
            if getattr(part, "part_kind", "") == "tool-return"
        ]
        names = [name for name, _ in returns]
        if "search_artifacts" not in names:
            yield {
                0: DeltaToolCall(
                    "search_artifacts", '{"query":"MIDDLE_NEEDLE"}', tool_call_id="search-call"
                )
            }
        elif "read_artifact" not in names:
            search_output = next(content for name, content in returns if name == "search_artifacts")
            hit = json.loads(search_output)["hits"][0]
            yield {
                0: DeltaToolCall(
                    "read_artifact",
                    json.dumps({"ref": hit["ref"], "start": hit["char_start"]}),
                    tool_call_id="read-call",
                )
            }
        else:
            read_output = next(content for name, content in returns if name == "read_artifact")
            assert "MIDDLE_NEEDLE" in read_output
            yield "closed the loop"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_stream),
        tools=manager.local_tools,
        toolsets=[],
        instructions="Search artifacts, then read the hit.",
        limits=LimitsConfig(),
        tool_metadata=manager.tool_metadata,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("read-only artifact tools should not request approval")

    outcome = await runtime.run(f"find the needle in {ref}", [], emit, approve)  # type: ignore[arg-type]

    assert outcome.output == "closed the loop"


async def test_model_recovers_from_invalid_max_results(tmp_path: Path) -> None:
    """An out-of-range argument comes back as a retryable tool error, not a crash."""
    manager = _manager(tmp_path)
    manager._artifact_store.store("recoverable search target body")
    seen_retry = False

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        nonlocal seen_retry
        retried = any(
            getattr(part, "part_kind", "") == "retry-prompt"
            and getattr(part, "tool_name", "") == "search_artifacts"
            for message in messages
            for part in getattr(message, "parts", [])
        )
        returned = any(
            getattr(part, "part_kind", "") == "tool-return"
            and getattr(part, "tool_name", "") == "search_artifacts"
            for message in messages
            for part in getattr(message, "parts", [])
        )
        if returned:
            yield "recovered"
        elif retried:
            seen_retry = True
            yield {0: DeltaToolCall("search_artifacts", '{"query":"recoverable"}', tool_call_id="ok")}
        else:
            yield {
                0: DeltaToolCall(
                    "search_artifacts",
                    '{"query":"recoverable","max_results":0}',
                    tool_call_id="bad",
                )
            }

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_stream),
        tools=manager.local_tools,
        toolsets=[],
        instructions="Search artifacts.",
        limits=LimitsConfig(),
        tool_metadata=manager.tool_metadata,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("read-only artifact tools should not request approval")

    outcome = await runtime.run("search", [], emit, approve)  # type: ignore[arg-type]

    assert seen_retry
    assert outcome.output == "recovered"
