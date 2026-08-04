"""Tests for the model-side artifact read-back tool (P0-2).

Receipts keep only head/tail excerpts of bulky tool outputs; ``read_artifact``
closes the loop so follow-up questions can page through the full body stored
under ``~/.lumen/artifacts`` (content-addressed, shared with the engine).
"""

# The unit tests deliberately exercise the private tool implementation and
# swap the store to a tmp path, matching the project's per-line-ignore style
# would add a dozen suppressions; file-level is cleaner here.
# pyright: reportPrivateUsage=false

from __future__ import annotations

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
version: 1
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


def test_receipt_points_at_read_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", inline_threshold_bytes=1)
    receipt, ref = store.build_receipt(
        tool_call_id="call-1",
        tool_name="run_command",
        content="x" * 100,
        status="success",
        summary="big output",
    )
    assert ref is not None
    assert f'retrieve: read_artifact(ref="{ref}")' in receipt


def test_read_artifact_pages_through_body(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    body = "0123456789" * 1000  # 10k chars
    ref = manager._artifact_store.store(body)

    first = manager._read_artifact(ref, start=0, max_chars=100)
    assert first.startswith(f"[artifact {ref} chars=10000 range=0-100]")
    assert body[:100] in first

    second = manager._read_artifact(ref, start=100, max_chars=50)
    assert "range=100-150" in second
    assert body[100:150] in second

    # Start beyond the end is safe and returns an empty slice.
    tail = manager._read_artifact(ref, start=20000, max_chars=10)
    assert "range=20000-10000" in tail

    # Reading the whole body across pages reconstructs it.
    pages = [manager._read_artifact(ref, start=i, max_chars=4000) for i in range(0, 10000, 4000)]
    reconstructed = "".join(page.split("\n", 1)[1] for page in pages)
    assert reconstructed == body


def test_read_artifact_error_kinds_are_recoverable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ValueError, match="invalid artifact ref"):
        manager._read_artifact("../escape")
    with pytest.raises(FileNotFoundError, match="artifact not found"):
        manager._read_artifact("sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="start must be >= 0"):
        manager._read_artifact("sha256:" + "0" * 64, start=-1)


async def test_model_can_call_read_artifact_tool(tmp_path: Path) -> None:
    """End-to-end: the registered tool is callable in a runtime run and the
    model receives the paged body as the tool result."""
    manager = _manager(tmp_path)
    ref = manager._artifact_store.store("full body the receipt truncated")
    assert "read_artifact" in {tool.name for tool in manager.local_tools}
    assert manager.tool_metadata["read_artifact"] == {
        "origin": "builtin:artifacts",
        "risk": "read",
    }

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
        called = any(
            part.part_kind == "tool-return" and part.tool_name == "read_artifact"  # type: ignore[attr-defined]
            for message in messages
            for part in getattr(message, "parts", [])
        )
        if not called:
            yield {0: DeltaToolCall("read_artifact", f'{{"ref":"{ref}"}}', tool_call_id="art-call")}
        else:
            yield "body retrieved"

    runtime = AgentRuntime(
        model=FunctionModel(stream_function=model_stream),
        tools=manager.local_tools,
        toolsets=[],
        instructions="Read artifacts when given a ref.",
        limits=LimitsConfig(),
        tool_metadata=manager.tool_metadata,
    )
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_request: object) -> ToolApproval:
        raise AssertionError("read-only artifact tool should not request approval")

    outcome = await runtime.run(f"read {ref}", [], emit, approve)  # type: ignore[arg-type]

    assert outcome.output == "body retrieved"
