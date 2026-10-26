from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import pytest
from test_reasoning import manager

from lumen.agents.types import AgentThreadRef, AgentThreadState
from lumen.config import SandboxConfig
from lumen.files.documents import read_workspace_document
from lumen.resources import ResourceManager
from lumen.tools.builtin import build_builtin_specs
from lumen.tools.web import build_download_file_spec
from lumen.tools.workspace import StaleWorkspaceResource, Workspace, WorkspaceViolation
from lumen.work_products import EffectStatus, TaskWorkspace, WorkProductStatus
from lumen.work_products.directories import DirectoryBundle, DirectoryResourceAdapter


@pytest.mark.parametrize("mode", ["workspace_write", "disabled"])
@pytest.mark.parametrize("approval", ["manual", "auto"])
async def test_config_controls_external_file_scope_independently_of_approval(
    tmp_path: Path, mode: Literal["workspace_write", "disabled"], approval: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    original = manager(root)
    config = original.config.model_copy(update={
        "sandbox": SandboxConfig(mode=mode),
        "permissions": original.config.permissions.model_copy(update={"default_mode": approval}),
    })
    resources = ResourceManager(config, workspace=root)
    session = resources.session_repository.create(agent_name="test", model_id="test")
    work = resources.task_workspace
    work.bind_session(session.id)
    specs = {spec.name: spec for spec in build_builtin_specs(
        root, sandbox_config=config.sandbox, task_workspace=work,
    )}
    outside = tmp_path / "external"
    outside.mkdir()
    target = outside / "note.txt"
    target.write_text("original\n")

    if mode == "workspace_write":
        for name, args in (
            ("read_file", (str(target),)), ("list_directory", (str(outside),)),
            ("search_text", ("original", str(outside))),
            ("write_file", (str(outside / "new.txt"), "new")),
            ("edit_file", (str(target), "original", "changed")),
        ):
            with pytest.raises(WorkspaceViolation):
                specs[name].function(*args)
        with pytest.raises(WorkspaceViolation):
            await specs["run_command"].function([sys.executable, "-V"], cwd=str(outside))
        with pytest.raises(WorkspaceViolation):
            work.open_work_product(str(target))
        assert target.read_text() == "original\n"
        return

    assert specs["read_file"].function(str(target))["content"] == "1: original"
    assert target.as_posix() in specs["list_directory"].function(str(outside))
    assert f"{target.as_posix()}:1:original" in specs["search_text"].function("original", str(outside))
    specs["edit_file"].function(str(target), "original", "changed")
    specs["write_file"].function("../external/new.txt", "new")
    with pytest.raises(FileExistsError):
        specs["write_file"].function(str(target), "overwritten")
    result = await specs["run_command"].function(
        [sys.executable, "-c", "from pathlib import Path; print(Path('note.txt').read_text())"],
        cwd=str(outside),
    )
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "changed"
    state = resources.session_repository.load(session.id).work_state
    assert {effect.resource for effect in state.effects} == {
        target.as_posix(), (outside / "new.txt").as_posix(),
    }
    assert all(effect.status is EffectStatus.VERIFIED for effect in state.effects)
    assert not work.completion_blockers()
    restored = TaskWorkspace(root, work.artifacts, work.repository, allow_external_paths=True)
    restored.bind_session(session.id)
    product = next(product for product in state.work_products if product.resource == target.as_posix())
    before = state.effects[0].before
    assert before is not None
    restored.restore_work_product(product.id, before.revision)
    assert target.read_text() == "original\n"
    assert not restored.completion_blockers()
    # Browser document endpoints keep their descriptor/no-follow workspace boundary.
    with pytest.raises(ValueError):
        read_workspace_document(root, str(target))


def test_full_access_keeps_symlink_and_revision_guards(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("original")
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    workspace = Workspace(root, allow_external_paths=True)
    assert workspace.resolve("../target.txt") == target
    with pytest.raises(WorkspaceViolation, match="symbolic link"):
        workspace.atomic_write(link / target.name, b"bad", expected_revision=workspace.revision(target))
    revision = workspace.revision(target)
    target.write_text("user changed")
    with pytest.raises(StaleWorkspaceResource, match="STALE_RESOURCE"):
        workspace.atomic_write(target, b"bad", expected_revision=revision)
    assert target.read_text() == "user changed"


async def test_external_download_uses_same_scope_and_journal(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    resources = manager(root)
    work = TaskWorkspace(
        root, resources.artifact_store, resources.session_repository, allow_external_paths=True,
    )
    session = resources.session_repository.create(agent_name="test", model_id="test")
    work.bind_session(session.id)
    target = tmp_path / "download.txt"
    spec = build_download_file_spec(
        root, task_workspace=work, host_guard=lambda _: True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="downloaded")),
    )
    await spec.function("https://example.com/source.txt", str(target))
    assert target.read_text() == "downloaded"
    state = resources.session_repository.load(session.id).work_state
    assert state.effects[0].status is EffectStatus.VERIFIED
    assert state.effects[0].resource == target.as_posix()


def test_full_access_cannot_replace_workspace_ancestors(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    adapter = DirectoryResourceAdapter(
        Workspace(tmp_path, allow_external_paths=True), resources.artifact_store,
    )
    with pytest.raises(WorkspaceViolation, match="ancestors"):
        adapter._publish(str(tmp_path.parent), DirectoryBundle(files={}), expected_revision="missing")  # pyright: ignore[reportPrivateUsage]


async def test_writable_child_remains_confined_with_full_parent_access(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    factory = resources.agent_runtime_factory
    factory.sandbox = SandboxConfig(mode="disabled")
    factory.enabled_builtins = ("read_file", "write_file", "run_command")
    factory.parent_tool_metadata = lambda: {
        name: {"origin": "builtin", "risk": risk, "effect": effect}
        for name, risk, effect in (
            ("read_file", "read", "observe"), ("write_file", "write", "mutation"),
            ("run_command", "execute", "execution"),
        )
    }
    config = factory.snapshot(resources.agent_profiles["worker"], approval_mode="auto")
    thread = AgentThreadState(
        ref=AgentThreadRef(
            id="child", path="/root/child", parent_session_id="session",
            root_run_id="run", agent_type="worker",
        ),
        task="edit", task_name="edit", config=config, idempotency_key="child",
    )
    cwd = tmp_path / "child"
    cwd.mkdir()
    tools, _ = factory._tools_for(thread, cwd)  # pyright: ignore[reportPrivateUsage]
    for tool in tools:
        function = cast(Callable[..., Any], tool.function)
        if tool.name in {"read_file", "write_file"}:
            args = ("../outside.txt", "bad") if tool.name == "write_file" else ("../outside.txt",)
            with pytest.raises(WorkspaceViolation):
                function(*args)
        elif tool.name == "run_command":
            with pytest.raises(WorkspaceViolation):
                await function([sys.executable, "-V"], cwd="..")


@pytest.mark.parametrize("uncertain_product", [False, True])
@pytest.mark.parametrize("status", [EffectStatus.APPLIED, EffectStatus.FAILED])
def test_revoking_full_access_keeps_interrupted_external_mutation_pending(
    tmp_path: Path, uncertain_product: bool, status: EffectStatus,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    resources = manager(root)
    work = TaskWorkspace(
        root, resources.artifact_store, resources.session_repository, allow_external_paths=True,
    )
    session = resources.session_repository.create(agent_name="test", model_id="test")
    work.bind_session(session.id)
    specs = {spec.name: spec for spec in build_builtin_specs(root, task_workspace=work)}
    target = tmp_path / "external.txt"
    specs["write_file"].function(str(target), "changed")
    state = work.repository.load(session.id).work_state
    work.repository.append_effect(
        session.id, state.effects[0].model_copy(update={
            "status": status, "verification": None, "error": "interrupted",
        }),
    )
    if uncertain_product:
        product = state.work_products[0].model_copy(
            update={"status": WorkProductStatus.RECONCILIATION_REQUIRED},
        )
        current = work.repository.load(session.id).work_state
        work.repository.append_work_state(session.id, current.upsert_product(product))
    revoked = TaskWorkspace(root, work.artifacts, work.repository)
    revoked.bind_session(session.id)
    pending = work.repository.load(session.id).work_state.effects[0]
    assert pending.status is (
        EffectStatus.RECONCILIATION_REQUIRED if status is EffectStatus.APPLIED else EffectStatus.FAILED
    )
    if status is EffectStatus.APPLIED:
        assert "escapes workspace" in (pending.error or "")
    assert all(not blocker.model_recoverable for blocker in revoked.completion_blockers())
    with pytest.raises(WorkspaceViolation):
        revoked.restore_work_product(state.work_products[0].id)
    assert target.read_text() == "changed"
    assert revoked.waive_effects(session.id, (), "user accepts unverified external state") == 1
    assert not revoked.completion_blockers()
    assert target.read_text() == "changed"


def test_standalone_tools_require_explicit_full_access(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    target = tmp_path / "external.txt"
    target.write_text("outside")
    for config in (None, SandboxConfig(), SandboxConfig(mode="disabled")):
        specs = {spec.name: spec for spec in build_builtin_specs(root, sandbox_config=config)}
        if config is not None and config.mode == "disabled":
            assert specs["read_file"].function(str(target))["content"] == "1: outside"
            specs["write_file"].function(str(target), "updated", overwrite=True)
            assert target.read_text() == "updated"
        else:
            with pytest.raises(WorkspaceViolation):
                specs["read_file"].function(str(target))
