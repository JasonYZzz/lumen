from __future__ import annotations

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
    assert policy.decide(edit, ApprovalMode.ACCEPT_EDITS).approved
    assert policy.decide(command, ApprovalMode.ACCEPT_EDITS).requires_confirmation
    assert policy.decide(mcp_write, ApprovalMode.ACCEPT_EDITS).requires_confirmation
    assert policy.decide(command, ApprovalMode.AUTO).approved
    assert policy.decide(unknown, ApprovalMode.AUTO).requires_confirmation


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
