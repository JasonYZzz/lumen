from __future__ import annotations

import json
from pathlib import Path

from lumen.trust import McpApprovalStore, TrustStore, canonical_project_identity


def test_trust_store_records_and_revokes_project_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "项目 with spaces"
    workspace.mkdir()
    path = tmp_path / "state" / "trust.json"
    store = TrustStore(path)

    assert store.is_trusted(workspace) is False
    project_id = store.trust(workspace)
    assert project_id == canonical_project_identity(workspace)
    assert store.is_trusted(workspace) is True
    assert json.loads(path.read_text(encoding="utf-8"))["projects"][project_id]["path"] == str(workspace)
    assert store.revoke(workspace) is True
    assert store.is_trusted(workspace) is False


def test_git_worktrees_share_project_identity(tmp_path: Path) -> None:
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    git_dir = common / "worktrees" / "feature"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..", encoding="utf-8")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}", encoding="utf-8")

    assert canonical_project_identity(tmp_path / "repo") == canonical_project_identity(worktree)


def test_mcp_approval_changes_when_fingerprint_changes_and_stores_no_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = McpApprovalStore(workspace, state_root=tmp_path / "state")

    store.set("database", "placeholder-fingerprint", "always")

    assert store.decision("database", "placeholder-fingerprint") == "always"
    assert store.decision("database", "changed-fingerprint") is None
    persisted = store.path.read_text(encoding="utf-8")
    assert "actual-database-password" not in persisted
    assert store.reset("database") is True
    assert store.decision("database", "placeholder-fingerprint") is None
