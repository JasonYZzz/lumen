from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from lumen.agent_loop import PydanticAIModelDriver
from lumen.config import ModelSettingsConfig, load_config
from lumen.resources import ResourceManager
from lumen.task_control import CONTROL_TOOL_NAMES


async def test_session_title_generator_is_a_tool_free_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text("version: 2\nagent: {model: {id: test}}\ntools: {builtins: []}\n")
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async def summarize(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.function_tools
        assert not info.output_tools
        assert len(messages) == 1
        return ModelResponse(parts=[TextPart("架构设计讨论")])

    def title_model(_config: ModelSettingsConfig) -> FunctionModel:
        return FunctionModel(summarize)

    monkeypatch.setattr("lumen.resources.build_model", title_model)
    assert await manager.generate_session_title("请讨论架构设计") == "架构设计讨论"


class _ToolWithDeprecatedSchemaAliases(SimpleNamespace):
    @property
    def inputSchema(self) -> object:
        raise AssertionError("deprecated inputSchema must not be accessed on SDK v2 tools")

    @property
    def outputSchema(self) -> object:
        raise AssertionError("deprecated outputSchema must not be accessed on SDK v2 tools")


@pytest.mark.parametrize("schema_style", ["v1", "v2", "v2_aliases", "missing"])
@pytest.mark.parametrize("has_output_schema", [False, True])
async def test_mcp_startup_preserves_schemas_without_accessing_deprecated_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_style: str,
    has_output_schema: bool,
) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """version: 2
agent: {model: {id: test}}
tools: {builtins: []}
mcp_servers:
  calc:
    transport: streamable_http
    url: http://127.0.0.1:1/mcp
    load_resources: false
    load_prompts: false
    tool_risks: {add: read}
""",
        encoding="utf-8",
    )
    parameters = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    returns = {"type": "object", "properties": {"result": {"type": "integer"}}} if has_output_schema else None
    remote_tool = (
        _ToolWithDeprecatedSchemaAliases(name="add", description="Add numbers")
        if schema_style == "v2_aliases"
        else SimpleNamespace(name="add", description="Add numbers")
    )
    if schema_style == "v1":
        remote_tool = SimpleNamespace(
            name="add", description="Add numbers", inputSchema=parameters, outputSchema=returns
        )
    elif schema_style != "missing":
        remote_tool.input_schema = parameters
        remote_tool.output_schema = returns
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)
    client = manager.mcp_bundles[0].client
    enter = AsyncMock(return_value=client)
    leave = AsyncMock()
    monkeypatch.setattr(client, "__aenter__", enter)
    monkeypatch.setattr(client, "__aexit__", leave)
    monkeypatch.setattr(client, "list_tools", AsyncMock(return_value=[remote_tool]))

    async with manager:
        assert manager.mcp_status["calc"] == "ok"
        descriptor = manager.capability_gateway.descriptor("calc_add")
        assert descriptor is not None
        assert descriptor.parameters == ({} if schema_style == "missing" else parameters)
        assert descriptor.requires_approval is False
        assert manager.runtime is not None
        assert '"calc": "ok"' in manager.runtime.instructions
        assert "MCP startup connection status" in manager.runtime.instructions
        # Both the model catalog and the executable capability must use the same schema.
        documents = [
            document
            for document in manager.runtime.tool_schema_documents
            if document.get("origin") == "mcp:calc"
        ]
        assert len(documents) == 1
        assert documents[0]["parameters"] == descriptor.parameters
        assert documents[0]["returns"] == (None if schema_style == "missing" else returns)

    enter.assert_awaited_once()
    leave.assert_awaited_once()
    assert manager.capability_gateway.descriptor("calc_add") is None


async def test_resource_manager_builds_runtime_and_selected_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  name: test-agent
  model:
    id: test
  limits: {}
tools:
  builtins: [read_file, search_text]
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.runtime is not None
        assert "framework: Lumen" in manager.runtime.instructions
        assert f"active_model_name: {manager.active_model_name()}" in manager.runtime.instructions
        assert "active_model_id: test" in manager.runtime.instructions
        assert set(manager.tool_metadata) >= {"read_file", "search_text"}
        assert CONTROL_TOOL_NAMES.issubset(manager.tool_metadata)
        for name in CONTROL_TOOL_NAMES:
            assert manager.tool_metadata[name]["control"] == "true"
        assert manager.session_repository is not None
        assert manager.runtime_invariant_report()["status"] == "ok"
        assert manager.summary()["invariants"]["status"] == "ok"


async def test_resource_manager_builds_single_lumen_loop_with_real_driver(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model: {id: test}
tools: {builtins: []}
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.runtime is not None
        assert isinstance(manager.runtime._model_driver, PydanticAIModelDriver)  # type: ignore[reportPrivateUsage]
        assert manager.runtime_invariant_report()["status"] == "ok"


async def test_capability_inventory_explains_visibility_policy_and_schema(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """version: 2
agent:
  model: {id: test}
tools:
  builtins: [read_file, write_file, run_command]
permissions:
  always_deny: [run_command]
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        inventory = {item["name"]: item for item in manager.capability_inventory()}

    assert inventory["read_file"]["status"] == "loaded"
    assert inventory["read_file"]["approval"] == "allow"
    assert inventory["read_file"]["concurrency"] == "parallel_safe"
    assert inventory["read_file"]["schema_digest"].startswith("sha256:")
    assert inventory["write_file"]["approval"] == "confirm"
    assert inventory["run_command"]["status"] == "disabled"
    assert inventory["run_command"]["approval"] == "deny"


async def test_resource_manager_appends_custom_instructions(tmp_path: Path) -> None:
    (tmp_path / "instructions.md").write_text("Always answer briefly.", encoding="utf-8")
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  instructions_file: instructions.md
  model:
    id: test
tools:
  builtins: []
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    assert "Always answer briefly." in manager.instructions
    assert "Do not reveal private chain-of-thought" in manager.instructions
    # Control instructions are appended by default.
    assert "set_plan" in manager.instructions
    assert "report_progress" in manager.instructions
    assert "outputs/" in manager.instructions


def test_base_instructions_define_lumen_without_impersonating_model_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model: {id: test}
tools: {builtins: []}
sessions: {directory: sessions}
""",
        encoding="utf-8",
    )

    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    assert "You are Lumen" in manager.instructions
    assert "Do not claim to be Claude" in manager.instructions
    assert "generated report" in manager.instructions


async def test_resource_manager_loads_plugin_relative_to_config(tmp_path: Path) -> None:
    (tmp_path / "project_tools.py").write_text(
        """
from lumen.tools import Risk, ToolSpec

def project_name() -> str:
    return "demo"

def create_tools() -> list[ToolSpec]:
    return [ToolSpec(project_name, risk=Risk.READ)]
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model:
    id: test
tools:
  builtins: []
  plugins:
    - module: project_tools
""",
        encoding="utf-8",
    )

    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.tool_metadata["project_name"]["origin"] == "plugin:project_tools"


async def test_capability_tools_have_builtin_origin_and_risk(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model:
    id: test
tools:
  builtins: [write_file, run_command]
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        assert manager.tool_metadata["write_file"]["risk"] == "write"
        assert manager.tool_metadata["run_command"]["risk"] == "execute"
        assert manager.tool_metadata["write_file"]["origin"] == "builtin"
        assert manager.tool_metadata["run_command"]["origin"] == "builtin"


async def test_resource_manager_tracks_mcp_health(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent:
  model:
    id: test
tools:
  builtins: []
mcp_servers:
  flaky:
    transport: streamable_http
    url: http://127.0.0.1:1/mcp
    required: false
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)
    assert manager.mcp_status == {"flaky": "connecting"}

    async with manager:
        # The optional server should have failed to connect without raising.
        assert manager.mcp_status["flaky"] == "error"
        assert any("flaky" in warning for warning in manager.warnings)
        assert manager.summary()["mcp_status"] == {"flaky": "error"}


async def test_resource_manager_summary_lists_control_tools(tmp_path: Path) -> None:
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(
        """
version: 2
agent: {model: {id: test}}
tools: {builtins: []}
""",
        encoding="utf-8",
    )
    manager = ResourceManager(load_config(config_path), workspace=tmp_path)

    async with manager:
        summary = manager.summary()
        for name in CONTROL_TOOL_NAMES:
            assert name in summary["tools"]
