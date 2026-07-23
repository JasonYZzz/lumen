from pathlib import Path

from typer.testing import CliRunner

from lumen.cli import app


def test_check_config_prints_discovered_tools(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(
        """
version: 1
agent:
  name: cli-test
  model:
    id: test
tools:
  builtins: [read_file]
sessions:
  directory: sessions
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config", str(config), "--cwd", str(tmp_path), "--check-config"])

    assert result.exit_code == 0
    assert "Configuration OK" in result.stdout
    assert "read_file" in result.stdout
