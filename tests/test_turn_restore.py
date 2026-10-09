from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from test_workspace_host import LocalResources, _runtime  # pyright: ignore[reportPrivateUsage]

from lumen.application import ApplyTurnRestore, InvalidStateError, PreviewTurnRestore, WorkspaceHost
from lumen.context import ArtifactStore
from lumen.work_products import TaskWorkspace


def resources_with_mutations(root: Path) -> tuple[LocalResources, str]:
    resources = LocalResources(root, _runtime(), artifact_store=ArtifactStore(root / "artifacts"))
    workspace = TaskWorkspace(root, resources.artifact_store, resources.session_repository)
    resources.task_workspace = workspace
    session = resources.session_repository.create(agent_name="test", model_id="offline")
    workspace.bind_session(session.id)
    for turn in range(2):
        interaction = f"run-{turn}"
        workspace.bind_interaction(interaction)
        for name in ("a.txt", "b.txt"):
            path = root / name
            if turn == 0:
                path.write_text("original\n")
            content = f"edited {turn}\n"
            workspace.perform_text_mutation(
                name, operation="write_file", selector="whole", change=content,
                action=lambda path=path, content=content: path.write_text(content),
            )
        resources.session_repository.append_turn(session.id, user_input=f"edit {turn}", messages=[],
            approvals=[], usage={}, status="completed", interaction_id=interaction)
    workspace.bind_interaction(None)
    return resources, session.id


async def test_restore_previews_precise_turn_files_requires_confirmation_and_is_idempotent(
    tmp_path: Path,
) -> None:
    resources, session_id = resources_with_mutations(tmp_path)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        preview = (await host.dispatch(PreviewTurnRestore(session_id, 1))).data
        assert len(preview["files"]) == 2 and preview["blockers"] == []
        assert "-edited 1" in preview["files"][0]["diff"]
        assert "+edited 0" in preview["files"][0]["diff"]
        command = ApplyTurnRestore(session_id, 1, preview["digest"], "restore")
        with pytest.raises(InvalidStateError, match="human confirmation"):
            await host.dispatch(command)
        command = ApplyTurnRestore(session_id, 1, preview["digest"], "restore", True)
        result = await host.dispatch(command)
        assert result.status == "restored"
        assert (tmp_path / "a.txt").read_text() == "edited 0\n"
        assert (tmp_path / "b.txt").read_text() == "edited 0\n"
        assert await host.dispatch(command) == result
        assert len(resources.session_repository.load(session_id).turns) == 2
    finally:
        await host.close()


async def test_stale_restore_preview_never_overwrites_user_changes(tmp_path: Path) -> None:
    resources, session_id = resources_with_mutations(tmp_path)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        preview = (await host.dispatch(PreviewTurnRestore(session_id, 0))).data
        (tmp_path / "b.txt").write_text("user modification\n")
        with pytest.raises(InvalidStateError, match=r"stale|conflicts"):
            await host.dispatch(ApplyTurnRestore(session_id, 0, preview["digest"], "restore", True))
        assert (tmp_path / "a.txt").read_text() == "edited 1\n"
        assert (tmp_path / "b.txt").read_text() == "user modification\n"
    finally:
        await host.close()


async def test_cancelled_restore_finishes_write_and_retains_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources, session_id = resources_with_mutations(tmp_path)
    workspace = resources.task_workspace
    assert workspace is not None
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original = workspace.restore_work_product
    def restore(product: str, revision: str | None = None) -> dict[str, Any]:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(product, revision)
    monkeypatch.setattr(workspace, "restore_work_product", restore)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    await host.open()
    try:
        preview = (await host.dispatch(PreviewTurnRestore(session_id, 1))).data
        command = ApplyTurnRestore(session_id, 1, preview["digest"], "cancelled-restore", True)
        task = asyncio.create_task(host.dispatch(command))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "a.txt").read_text() == "edited 0\n"
        assert (await host.dispatch(command)).status == "restored"
    finally:
        release.set()
        await host.close()


def test_web_restore_and_journal_repair_share_explicit_confirmation_contract(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from lumen.api import create_web_app
    resources, session_id = resources_with_mutations(tmp_path)
    host = WorkspaceHost(resources)  # type: ignore[arg-type]
    with TestClient(create_web_app(host, launch_token="test", api_only=True),
                    base_url="http://testserver") as client:
        client.get("/auth/exchange", params={"token": "test"})
        preview = client.get(f"/api/v1/sessions/{session_id}/restore-files?before_turn=1").json()
        body = {"before_turn": 1, "expected_digest": preview["digest"], "client_request_id": "restore"}
        url = f"/api/v1/sessions/{session_id}/restore-files"
        headers = {"Origin": "http://testserver"}
        assert client.post(url, json=body, headers=headers).status_code == 400
        result = client.post(url, json={**body, "confirmed": True}, headers=headers)
        assert result.json()["status"] == "restored"
        path = resources.session_repository.load(session_id).metadata.path
        with path.open("ab") as journal:
            journal.write(b'{"type":"turn')
        repair_url = f"/api/v1/sessions/{session_id}/repair"
        inspection = client.get(repair_url).json()
        body = {"expected_digest": inspection["original_digest"]}
        assert client.post(repair_url, json=body, headers=headers).status_code == 400
        result = client.post(repair_url, json={**body, "confirmed": True}, headers=headers)
        assert result.json()["status"] == "repaired"
