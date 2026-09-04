from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from lumen.config import SandboxConfig
from lumen.sandbox import SandboxRunner, SandboxUnavailableError
from lumen.tools.capability import build_capability_specs


def test_unresolvable_executable_cleans_temporary_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    def make_temp(**_kwargs: object) -> str:
        return str(scratch)

    monkeypatch.setattr("lumen.sandbox.tempfile.mkdtemp", make_temp)
    with pytest.raises(FileNotFoundError):
        SandboxRunner(tmp_path, SandboxConfig()).prepare(["/missing/lumen-executable"], cwd=tmp_path)
    assert not scratch.exists()


def test_missing_sandbox_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lumen.sandbox.platform.system", lambda: "unsupported")
    with pytest.raises(SandboxUnavailableError):
        SandboxRunner(tmp_path, SandboxConfig()).prepare([sys.executable, "-V"], cwd=tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration")
@pytest.mark.parametrize("executable", ["/usr/bin/curl", "/usr/bin/python3", sys.executable])
async def test_seatbelt_allows_offline_system_runtimes(tmp_path: Path, executable: str) -> None:
    command = next(
        spec for spec in build_capability_specs(tmp_path, max_timeout=10, sandbox_config=SandboxConfig())
        if spec.name == "run_command"
    )
    args = ["--version"] if executable.endswith("curl") else [
        "-c", "import ssl, tempfile; f = tempfile.TemporaryFile(); f.write(b'ok'); "
        "f.seek(0); print(f.read().decode())",
    ]
    result = await command.function([executable, *args])
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"]
    assert result["sandbox"]["network_allowed"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration")
@pytest.mark.parametrize("network", [False, True])
async def test_seatbelt_network_toggle_remains_enforced(tmp_path: Path, network: bool) -> None:
    command = next(
        spec for spec in build_capability_specs(
            tmp_path, max_timeout=5, sandbox_config=SandboxConfig(network=network),
        ) if spec.name == "run_command"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = await command.function([
            sys.executable, "-c",
            f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=1)",
        ])
    assert (result["exit_code"] == 0) is network
    assert result["sandbox"]["network_allowed"] is network


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration")
async def test_seatbelt_keeps_private_files_and_journal_protected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("private")
    protected = workspace / ".lumen"
    protected.mkdir()
    journal = protected / "journal"
    journal.write_text("original")
    command = next(
        spec for spec in build_capability_specs(workspace, max_timeout=5, sandbox_config=SandboxConfig())
        if spec.name == "run_command"
    )
    for operation in [f"open({str(private)!r}).read()", f"open({str(journal)!r}, 'w').write('changed')"]:
        result = await command.function([sys.executable, "-c", operation])
        assert result["exit_code"] != 0
        assert "PermissionError" in result["stderr"]
    assert journal.read_text() == "original"


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt policy")
def test_seatbelt_does_not_grant_etc_or_private_keys(tmp_path: Path) -> None:
    prepared = SandboxRunner(tmp_path, SandboxConfig()).prepare([sys.executable, "-V"], cwd=tmp_path)
    try:
        profile = prepared.argv[2]
        assert '(allow file-read* (subpath "/private/etc"))' not in profile
        assert '(allow file-read* (subpath "/private/etc/ssl"))' not in profile
        assert '(allow file-read* (subpath "/private/etc/ssl/private"))' not in profile
        assert '(allow network*)' not in profile
    finally:
        prepared.cleanup()
