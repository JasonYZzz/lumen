import asyncio
import os
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import LimitsConfig, load_config
from lumen.events import RunEvent, ToolCallFinished
from lumen.resources import ResourceManager, ResourceStartupError
from lumen.runtime import AgentRuntime, ToolApproval


def last_tool_return(messages: Sequence[ModelMessage]) -> ToolReturnPart | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part
    return None


async def test_stdio_mcp_server_is_discovered(tmp_path: Path) -> None:
    server = Path(__file__).parent / "fixtures/calculator_mcp.py"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"""
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  calc:
    transport: stdio
    command: {sys.executable!r}
    args: [{str(server)!r}]
    tool_risks:
      add: read
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.tool_metadata["calc_add"]["risk"] == "read"

        async def model_stream(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
            calc = next(tool for tool in info.function_tools if tool.name == "calc_add")
            # FunctionModel exposes the native tool-search corpus directly;
            # real providers either use native search or the local fallback.
            assert calc.defer_loading is True
            assert calc.with_native == "tool_search"
            if last_tool_return(messages) is None:
                yield {0: DeltaToolCall("calc_add", '{"a":2,"b":3}', tool_call_id="mcp-call")}
            else:
                yield "calculation complete"

        runtime = AgentRuntime(
            model=FunctionModel(stream_function=model_stream),
            tools=[],
            toolsets=[manager.mcp_bundles[0].toolset],
            instructions="Use the calculator.",
            limits=LimitsConfig(),
            tool_metadata=manager.tool_metadata,
        )
        events: list[RunEvent] = []

        async def emit(event: RunEvent) -> None:
            events.append(event)

        async def approve(_request: object) -> ToolApproval:
            raise AssertionError("read-only MCP tool should not request approval")

        outcome = await runtime.run("add two and three", [], emit, approve)  # type: ignore[arg-type]

        assert outcome.output == "calculation complete"
        assert any(isinstance(event, ToolCallFinished) and not event.is_error for event in events)


async def test_unknown_tool_risk_name_is_rejected_at_startup(tmp_path: Path) -> None:
    server = Path(__file__).parent / "fixtures/calculator_mcp.py"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"""
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  calc:
    transport: stdio
    command: {sys.executable!r}
    args: [{str(server)!r}]
    tool_risks:
      misspelled_lookup: read
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    with pytest.raises(ResourceStartupError, match=r"unknown tool_risks.*misspelled_lookup"):
        await manager.open()


async def test_unclassified_mcp_tools_emit_actionable_warning(tmp_path: Path) -> None:
    server = Path(__file__).parent / "fixtures/calculator_mcp.py"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"""
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  calc:
    transport: stdio
    command: {sys.executable!r}
    args: [{str(server)!r}]
    tool_risks: {{}}
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert any(
            "unclassified tools default to external_unknown" in warning and "add" in warning
            for warning in manager.warnings
        )


async def test_streamable_http_mcp_server_is_discovered(tmp_path: Path) -> None:
    server = Path(__file__).parent / "fixtures/calculator_mcp.py"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    environment = {
        **os.environ,
        "MCP_TEST_TRANSPORT": "streamable-http",
        "MCP_TEST_PORT": str(port),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(server),
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            with socket.socket() as client:
                if client.connect_ex(("127.0.0.1", port)) == 0:
                    break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("HTTP MCP fixture did not start")

        config_path = tmp_path / "agent.yaml"
        config_path.write_text(
            f"""
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  calc_http:
    transport: streamable_http
    url: http://127.0.0.1:{port}/mcp
    tool_risks:
      add: read
""",
            encoding="utf-8",
        )
        manager = ResourceManager(load_config(config_path), workspace=tmp_path)

        async with manager:
            assert manager.tool_metadata["calc_http_add"]["risk"] == "read"
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)


async def test_server_side_tool_error_feeds_back_to_model(tmp_path: Path) -> None:
    """A tool that fails *on the server* must surface as a model retry, not a
    terminated run: pydantic-ai converts the MCP error result to ``ModelRetry``
    and the loop continues (pinned here so an upgrade cannot silently regress).
    """
    server = Path(__file__).parent / "fixtures/calculator_mcp.py"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"""
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  calc:
    transport: stdio
    command: {sys.executable!r}
    args: [{str(server)!r}]
    tool_risks:
      explode: read
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:

        async def model_stream(messages: list[ModelMessage], info: AgentInfo):  # type: ignore[no-untyped-def]
            saw_retry = any(
                isinstance(message, ModelRequest)
                and any(isinstance(part, RetryPromptPart) for part in message.parts)
                for message in messages
            )
            if not saw_retry:
                yield {0: DeltaToolCall("calc_explode", "{}", tool_call_id="mcp-fail")}
            else:
                yield "recovered from server error"

        runtime = AgentRuntime(
            model=FunctionModel(stream_function=model_stream),
            tools=[],
            toolsets=[manager.mcp_bundles[0].toolset],
            instructions="Use the calculator.",
            limits=LimitsConfig(),
            tool_metadata=manager.tool_metadata,
        )
        events: list[RunEvent] = []

        async def emit(event: RunEvent) -> None:
            events.append(event)

        async def approve(_request: object) -> ToolApproval:
            raise AssertionError("read-only MCP tool should not request approval")

        outcome = await runtime.run("trigger the failing tool", [], emit, approve)  # type: ignore[arg-type]

        assert outcome.output == "recovered from server error"


async def test_session_close_tolerates_reconnect_drained_client(tmp_path: Path) -> None:
    """When the runtime reconnect path leaves a client's enter/exit count
    drained (server stayed dead), session teardown must still close cleanly.
    Simulated here by manually exiting the persisted client once.
    """
    server = Path(__file__).parent / "fixtures/calculator_mcp.py"
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        f"""
version: 1
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  calc:
    transport: stdio
    command: {sys.executable!r}
    args: [{str(server)!r}]
    tool_risks:
      add: read
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        # Drain the persisted hold as a failed reconnect would.
        await manager.mcp_bundles[0].client.__aexit__(None, None, None)
    # Exiting the context above must not raise ValueError from the final exit.
