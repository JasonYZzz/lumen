from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import Tool
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from typer.testing import CliRunner

import lumen.cli as cli
from lumen.approval import ApprovalMode
from lumen.cli import app
from lumen.config import LimitsConfig
from lumen.headless import HeadlessResult, run_headless
from lumen.runtime import AgentRuntime
from lumen.sessions import SessionRepository


class LocalResources:
    """Minimal WorkspaceResources stand-in, mirroring test_workspace_host."""

    def __init__(self, root: Path, runtime: AgentRuntime) -> None:
        self.workspace = root
        self.session_repository = SessionRepository(root / "sessions")
        self.runtime: AgentRuntime | None = runtime
        self.config = SimpleNamespace(
            agent=SimpleNamespace(name="test-agent"),
            permissions=SimpleNamespace(default_mode="manual"),
        )
        self.tool_metadata: dict[str, dict[str, str]] = {}
        self.warnings: list[str] = []
        self.skills: list[object] = []
        self.agent_orchestrator: Any = None
        self.configuration: Any = None

    async def open(self) -> LocalResources:
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

    def context_source_summary(self, session_id: str) -> list[dict[str, str]]:
        del session_id
        return []

    def mcp_prompt_summary(self) -> list[dict[str, object]]:
        return []

    async def render_mcp_prompt(self, reference: str, arguments: dict[str, str]) -> str:
        del reference, arguments
        return ""

    def hook_summary(self) -> list[dict[str, object]]:
        return []

    def capabilities_report(self) -> dict[str, object]:
        return {"tools": [], "skills": [], "mcp_servers": [], "agent_profiles": []}

    def summary(self) -> dict[str, object]:
        return {"agent": "test-agent", "model": "test-model"}


def _runtime(
    stream: Any,
    *,
    tools: list[Tool] | None = None,
    tool_metadata: dict[str, dict[str, str]] | None = None,
) -> AgentRuntime:
    return AgentRuntime(
        model=FunctionModel(stream_function=stream),
        tools=tools or [],
        toolsets=[],
        instructions="help",
        limits=LimitsConfig(),
        tool_metadata=tool_metadata or {},
    )


def _text_runtime(output: str) -> AgentRuntime:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        yield output

    return _runtime(stream)


def _approval_runtime(executed: list[bool]) -> AgentRuntime:
    def write_note(content: str) -> str:
        executed.append(True)
        return content

    async def stream(messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        tool_return = _last_tool_return(messages)
        if tool_return is None:
            yield {0: DeltaToolCall("write_note", '{"content":"safe"}', tool_call_id="call-1")}
        else:
            yield f"handled:{tool_return.outcome}"

    return _runtime(
        stream,
        tools=[Tool(write_note, sequential=True, requires_approval=True)],
        tool_metadata={"write_note": {"origin": "plugin:test", "risk": "write"}},
    )


def _last_tool_return(messages: list[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


async def test_headless_text_streams_deltas_and_persists_session(tmp_path: Path) -> None:
    out = io.StringIO()
    resources = LocalResources(tmp_path, _text_runtime("final answer"))

    result = await run_headless(resources, "explain this repo", stdout=out)

    assert result.error is None
    assert result.exit_code() == 0
    assert result.text == "final answer"
    assert out.getvalue() == "final answer\n"
    # Same JSONL persistence as the TUI: the turn is resumable afterwards.
    loaded = resources.session_repository.load(result.session_id)
    assert [turn.user_input for turn in loaded.turns] == ["explain this repo"]
    assert loaded.turns[0].status == "completed"


async def test_headless_resume_continues_an_existing_session(tmp_path: Path) -> None:
    resources = LocalResources(tmp_path, _text_runtime("second answer"))
    first = await run_headless(resources, "first question", stdout=io.StringIO())

    resumed = await run_headless(resources, "follow up", resume_id=first.session_id, stdout=io.StringIO())

    assert resumed.error is None
    assert resumed.session_id == first.session_id
    loaded = resources.session_repository.load(first.session_id)
    assert [turn.user_input for turn in loaded.turns] == ["first question", "follow up"]


async def test_headless_json_collects_one_structured_document(tmp_path: Path) -> None:
    out = io.StringIO()
    resources = LocalResources(tmp_path, _text_runtime("json answer"))

    result = await run_headless(resources, "hi", output_format="json", stdout=out)

    payload = json.loads(out.getvalue())
    assert payload["type"] == "result"
    assert payload["subtype"] == "success"
    assert payload["is_error"] is False
    assert payload["result"] == "json answer"
    assert payload["session_id"] == result.session_id
    assert payload["model"] == "test-model"
    assert payload["num_turns"] == 1
    assert payload["error"] is None
    assert isinstance(payload["usage"], dict)


async def test_headless_denies_approval_gated_tools_by_default(tmp_path: Path) -> None:
    executed: list[bool] = []
    out = io.StringIO()
    resources = LocalResources(tmp_path, _approval_runtime(executed))

    result = await run_headless(resources, "write a note", stdout=out)

    assert result.error is None
    assert result.exit_code() == 0
    assert executed == []
    # The model received the denial and produced a final answer anyway.
    assert "handled:" in result.text
    turn = resources.session_repository.load(result.session_id).turns[0]
    assert turn.approvals[0]["approved"] is False


async def test_headless_permission_mode_auto_approves_tool_calls(tmp_path: Path) -> None:
    executed: list[bool] = []
    resources = LocalResources(tmp_path, _approval_runtime(executed))

    result = await run_headless(
        resources,
        "write a note",
        permission_mode=ApprovalMode.AUTO,
        stdout=io.StringIO(),
    )

    assert result.error is None
    assert executed == [True]
    assert result.text.startswith("handled:")


async def test_headless_model_error_sets_error_and_nonzero_exit(tmp_path: Path) -> None:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        raise RuntimeError("model exploded")
        yield "unreachable"

    out = io.StringIO()
    resources = LocalResources(tmp_path, _runtime(stream))

    result = await run_headless(resources, "boom", stdout=out)

    assert result.error is not None
    assert "model exploded" in result.error
    assert result.exit_code() == 1
    # The failure turn is still persisted, matching TUI behavior.
    loaded = resources.session_repository.load(result.session_id)
    assert loaded.turns[0].status == "failed"


async def test_headless_json_reports_errors_in_the_document(tmp_path: Path) -> None:
    async def stream(_messages: list[ModelMessage], _info: AgentInfo):  # type: ignore[no-untyped-def]
        raise RuntimeError("usage limit reached")
        yield "unreachable"

    out = io.StringIO()
    resources = LocalResources(tmp_path, _runtime(stream))

    result = await run_headless(resources, "boom", output_format="json", stdout=out)

    payload = json.loads(out.getvalue())
    assert result.exit_code() == 1
    assert payload["subtype"] == "error"
    assert payload["is_error"] is True
    assert "usage limit reached" in payload["error"]


def _minimal_config() -> str:
    return """version: 2
agent:
  name: cli-test
  model:
    id: test
tools:
  builtins: [read_file]
"""


def test_cli_print_and_check_config_are_mutually_exclusive(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--print", "hi", "--check-config", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "cannot be combined" in result.output


def test_cli_output_format_requires_print(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--output-format", "json", "--cwd", str(tmp_path)])

    assert result.exit_code == 1
    assert "require --print" in result.output


@pytest.mark.parametrize("mode", ["plan", "yolo"])
def test_cli_rejects_non_approval_mode(tmp_path: Path, mode: str) -> None:
    result = CliRunner().invoke(
        app, ["--print", "hi", "--permission-mode", mode, "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "--permission-mode must be one of: manual, accept_edits, auto" in result.output


def test_cli_print_exits_nonzero_and_reports_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(_minimal_config(), encoding="utf-8")

    async def fake_run(*_args: object, **_kwargs: object) -> HeadlessResult:
        return HeadlessResult(session_id="s-1", error="usage limit reached")

    monkeypatch.setattr(cli, "run_headless", fake_run)
    result = CliRunner().invoke(
        app, ["--print", "hi", "--config", str(config), "--cwd", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert "usage limit reached" in result.stderr
