from __future__ import annotations

from pathlib import Path

from lumen.approval import ApprovalMode, ApprovalPolicy, ApprovalPresenter
from lumen.events import ApprovalRequest


def _request(
    name: str,
    risk: str,
    *,
    origin: str = "builtin",
    args: dict[str, object] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        call_id="call-1",
        name=name,
        args=dict(args or {}),
        origin=origin,
        risk=risk,
    )


def test_approval_policy_supports_manual_accept_edits_and_auto() -> None:
    policy = ApprovalPolicy()
    edit = _request("edit_file", "write")
    command = _request("run_command", "execute")
    mcp_write = _request("crm_update", "write", origin="mcp:crm")
    unknown = _request("crm_delete", "external_unknown", origin="mcp:crm")

    assert policy.decide(edit, ApprovalMode.MANUAL).requires_confirmation
    assert policy.decide(_request("read_file", "read"), ApprovalMode.MANUAL).approved
    assert policy.decide(edit, ApprovalMode.ACCEPT_EDITS).approved
    assert policy.decide(command, ApprovalMode.ACCEPT_EDITS).requires_confirmation
    assert policy.decide(mcp_write, ApprovalMode.ACCEPT_EDITS).requires_confirmation
    assert policy.decide(command, ApprovalMode.AUTO).approved
    assert policy.decide(unknown, ApprovalMode.AUTO).requires_confirmation


def test_approval_policy_classifies_plan_reads_and_accept_edits_boundaries() -> None:
    policy = ApprovalPolicy()
    inspect = _request("run_command", "execute", args={"argv": ["git", "diff", "--stat"]})
    mutate = _request("run_command", "execute", args={"argv": ["git", "reset", "--hard"]})
    local_mkdir = _request("run_command", "execute", args={"argv": ["mkdir", "outputs"]})
    escaping_mkdir = _request("run_command", "execute", args={"argv": ["mkdir", "../outside"]})

    assert policy.is_read_only(inspect)
    assert not policy.is_read_only(mutate)
    assert policy.decide(local_mkdir, ApprovalMode.ACCEPT_EDITS).approved
    assert policy.decide(escaping_mkdir, ApprovalMode.ACCEPT_EDITS).requires_confirmation


def test_auto_keeps_protected_configuration_edits_approval_gated() -> None:
    decision = ApprovalPolicy().decide(
        _request("write_file", "write", args={"path": ".git/config", "content": "x"}),
        ApprovalMode.AUTO,
    )

    assert decision.requires_confirmation


def test_auto_cannot_bypass_writable_child_spawn_approval() -> None:
    decision = ApprovalPolicy().decide(
        _request("spawn_child", "execute", args={"task": "edit", "kind": "worktree"}),
        ApprovalMode.AUTO,
    )

    assert decision.requires_confirmation
    assert "always require" in decision.message


def test_approval_presenter_exposes_command_before_cwd() -> None:
    view = ApprovalPresenter().build(
        _request(
            "run_command",
            "execute",
            args={"argv": ["uv", "run", "pytest", "-q"], "cwd": "."},
        )
    )

    assert "$ uv run pytest -q" in view.preview
    assert "cwd: ." in view.preview
    assert view.preview.index("$ uv run pytest -q") < view.preview.index("cwd: .")


def test_approval_presenter_exposes_edit_diff_and_write_content() -> None:
    presenter = ApprovalPresenter()
    edit = presenter.build(
        _request(
            "edit_file",
            "write",
            args={"path": "src/a.py", "old_text": "old\n", "new_text": "new\n"},
        )
    )
    write = presenter.build(
        _request(
            "write_file",
            "write",
            args={"path": "out.txt", "content": "secret-safe-preview"},
        )
    )

    assert "-old" in edit.preview
    assert "+new" in edit.preview
    assert "secret-safe-preview" in write.preview
    assert "19 bytes" in write.preview


def test_approval_presenter_write_file_overwrite_diffs_against_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("before\nkeep\n", encoding="utf-8")
    presenter = ApprovalPresenter(workspace_root=tmp_path)

    view = presenter.build(
        _request(
            "write_file",
            "write",
            args={"path": "out.txt", "content": "after\nkeep\n", "overwrite": True},
        )
    )

    assert "-before" in view.preview
    assert "+after" in view.preview
    assert " keep" in view.preview or "keep" in view.preview


def test_approval_presenter_write_file_new_file_is_all_additions(tmp_path: Path) -> None:
    presenter = ApprovalPresenter(workspace_root=tmp_path)

    view = presenter.build(
        _request(
            "write_file",
            "write",
            args={"path": "fresh.txt", "content": "line one\nline two\n"},
        )
    )

    assert "+line one" in view.preview
    assert "+line two" in view.preview
    assert "\n-line" not in view.preview


def test_approval_presenter_write_file_escaping_path_falls_back_to_dump(tmp_path: Path) -> None:
    presenter = ApprovalPresenter(workspace_root=tmp_path)

    view = presenter.build(
        _request(
            "write_file",
            "write",
            args={"path": "../outside.txt", "content": "raw dump body", "overwrite": True},
        )
    )

    assert "raw dump body" in view.preview
    assert "+raw dump body" not in view.preview
