from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from lumen.config import SandboxConfig
from lumen.tools.git import GitCapabilityError, build_git_specs


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Lumen Test")
    _git(root, "config", "user.email", "lumen@example.invalid")
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")


def _tools(root: Path, *, sandbox: SandboxConfig | None = None) -> dict[str, Callable[..., Any]]:
    return {
        cast(str, spec.name): spec.function
        for spec in build_git_specs(
            root,
            max_timeout=10,
            sandbox_config=sandbox or SandboxConfig(mode="disabled"),
        )
    }


async def test_structured_git_stage_and_commit_verify_reviewed_state(tmp_path: Path) -> None:
    _repository(tmp_path)
    tools = _tools(tmp_path)
    status = await tools["git_status"]()
    (tmp_path / "tracked.txt").write_text("after\n", encoding="utf-8")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch hook-ran\n", encoding="utf-8")
    hook.chmod(0o700)
    filter_script = tmp_path / "filter.sh"
    filter_script.write_text("#!/bin/sh\ntouch filter-ran\ncat\n", encoding="utf-8")
    filter_script.chmod(0o700)
    (tmp_path / ".gitattributes").write_text("tracked.txt filter=unsafe\n", encoding="utf-8")
    _git(tmp_path, "config", "filter.unsafe.clean", str(filter_script))

    staged = await tools["git_stage"](["tracked.txt"], status["head"])
    committed = await tools["git_commit"](
        "structured commit",
        status["head"],
        staged["index_fingerprint"],
    )

    assert committed["head"] == _git(tmp_path, "rev-parse", "HEAD")
    assert _git(tmp_path, "log", "-1", "--pretty=%s") == "structured commit"
    assert not (tmp_path / "hook-ran").exists()
    assert not (tmp_path / "filter-ran").exists()
    assert _git(tmp_path, "show", ":tracked.txt") == "after"
    assert _git(tmp_path, "status", "--porcelain", "--", "tracked.txt") == ""


async def test_structured_git_ignores_system_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository(tmp_path)
    expected_head = _git(tmp_path, "rev-parse", "HEAD")
    config = tmp_path / "system.gitconfig"
    config.write_text("[invalid\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
    # Explicitly pass the system-config path through the test allow-list so
    # this checks NOSYSTEM rather than ordinary environment sanitization.
    tools = _tools(tmp_path, sandbox=SandboxConfig(
        mode="disabled", env_allow=["PATH", "GIT_CONFIG_SYSTEM"],
    ))
    status = await tools["git_status"]()
    assert status["head"] == expected_head
    assert status["index_fingerprint"].startswith("sha256:")


async def test_structured_git_diff_and_remote_projection_are_read_only_and_secret_safe(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    _git(tmp_path, "remote", "add", "origin", "https://token@example.com/acme/repo.git?secret=yes")
    tools = _tools(tmp_path)
    status = await tools["git_status"]()
    (tmp_path / "tracked.txt").write_text("after\n", encoding="utf-8")
    await tools["git_stage"](["tracked.txt"], status["head"])

    diff = await tools["git_diff"](True, ["tracked.txt"])
    refreshed = await tools["git_status"]()

    assert "+after" in diff["diff"]
    assert refreshed["remotes"][0]["url"] == "https://example.com/acme/repo.git"
    assert "token" not in str(refreshed)
    assert "secret=yes" not in str(refreshed)


async def test_structured_git_stage_supports_explicit_deletion(tmp_path: Path) -> None:
    _repository(tmp_path)
    tools = _tools(tmp_path)
    status = await tools["git_status"]()
    (tmp_path / "tracked.txt").unlink()

    staged = await tools["git_stage"](["tracked.txt"], status["head"])
    diff = await tools["git_diff"](True, ["tracked.txt"])

    assert "deleted file mode" in diff["diff"]
    committed = await tools["git_commit"](
        "delete tracked file",
        status["head"],
        staged["index_fingerprint"],
    )
    assert committed["head"] == _git(tmp_path, "rev-parse", "HEAD")
    assert _git(tmp_path, "ls-files", "tracked.txt") == ""


async def test_structured_git_rejects_stale_head_and_unsafe_push_remote(tmp_path: Path) -> None:
    _repository(tmp_path)
    tools = _tools(tmp_path)
    status = await tools["git_status"]()
    (tmp_path / "second.txt").write_text("second\n", encoding="utf-8")
    _git(tmp_path, "add", "second.txt")
    _git(tmp_path, "commit", "-qm", "second")

    with pytest.raises(GitCapabilityError, match="HEAD changed after review"):
        await tools["git_stage"](["tracked.txt"], status["head"])

    _git(tmp_path, "remote", "add", "unsafe", "ext::sh -c touch% /tmp/owned")
    refreshed = await tools["git_status"]()
    remote = next(item for item in refreshed["remotes"] if item["name"] == "unsafe")
    with pytest.raises(GitCapabilityError, match="HTTPS or SSH"):
        await tools["git_push"](
            "unsafe",
            "main",
            refreshed["head"],
            remote["url"],
            remote["fingerprint"],
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt integration")
async def test_structured_git_can_write_control_state_without_releasing_workspace_sandbox(
    tmp_path: Path,
) -> None:
    _repository(tmp_path)
    tools = _tools(tmp_path, sandbox=SandboxConfig())
    filter_script = tmp_path / "filter.sh"
    filter_script.write_text("#!/bin/sh\ntouch filter-ran\ncat\n", encoding="utf-8")
    filter_script.chmod(0o700)
    (tmp_path / ".gitattributes").write_text("tracked.txt filter=unsafe\n", encoding="utf-8")
    _git(tmp_path, "config", "filter.unsafe.clean", str(filter_script))
    (tmp_path / "tracked.txt").write_text("sandboxed\n", encoding="utf-8")
    status = await tools["git_status"]()

    assert not (tmp_path / "filter-ran").exists()

    staged = await tools["git_stage"](["tracked.txt"], status["head"])
    committed = await tools["git_commit"](
        "sandboxed structured commit",
        status["head"],
        staged["index_fingerprint"],
    )

    assert committed["head"] == _git(tmp_path, "rev-parse", "HEAD")
