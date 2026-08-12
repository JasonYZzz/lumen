from pathlib import Path

from lumen.config import load_config
from lumen.resources import CONTROL_TOOL_NAMES, ResourceManager


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
