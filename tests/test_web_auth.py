from __future__ import annotations

import os
from pathlib import Path

import pytest

from lumen.api.auth import web_session_secret
from lumen.tools.workspace import WorkspaceViolation


def test_web_credentials_survive_restart_and_isolate_endpoints_and_workspaces(tmp_path: Path) -> None:
    first = web_session_secret(tmp_path, "127.0.0.1:8765")
    assert web_session_secret(tmp_path, "127.0.0.1:8765") == first
    assert web_session_secret(tmp_path, "127.0.0.1:8766") != first
    assert web_session_secret(tmp_path / "other", "127.0.0.1:8765") != first
    if os.name == "posix":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in tmp_path.glob(".lumen/*.key"))


def test_web_credentials_reject_corruption_without_replacing_key(tmp_path: Path) -> None:
    web_session_secret(tmp_path, "127.0.0.1:8765")
    key = next(tmp_path.glob(".lumen/*.key"))
    key.write_text("corrupt")
    with pytest.raises(ValueError, match="invalid Web"):
        web_session_secret(tmp_path, "127.0.0.1:8765")
    assert key.read_text() == "corrupt"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes and symlinks")
def test_web_credentials_reject_public_key_and_symlink_directory(tmp_path: Path) -> None:
    web_session_secret(tmp_path, "127.0.0.1:8765")
    key = next(tmp_path.glob(".lumen/*.key"))
    key.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        web_session_secret(tmp_path, "127.0.0.1:8765")
    other = tmp_path / "other"
    other.mkdir()
    (other / ".lumen").symlink_to(tmp_path / ".lumen", target_is_directory=True)
    with pytest.raises(WorkspaceViolation, match="symbolic link"):
        web_session_secret(other, "127.0.0.1:8765")
