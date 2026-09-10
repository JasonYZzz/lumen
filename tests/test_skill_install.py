from __future__ import annotations

import asyncio
import io
import json
import stat
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from lumen.config import load_config
from lumen.context.artifacts import ArtifactStore
from lumen.events import ApprovalRequest, RunEvent
from lumen.resources import ResourceManager
from lumen.runtime import ToolApproval
from lumen.sessions import SessionRepository
from lumen.skill_install import SkillInstaller
from lumen.skills import SkillLoader
from lumen.tools.gateway import CapabilityApproval, CapabilityInvocation
from lumen.work_products import EffectStatus, TaskWorkspace

SHA = "a" * 40
SKILL = (
    b"---\nname: sample-skill\ndescription: Test complete skill installation\n---\nUse references/help.md.\n"
)


def archive(files: dict[str, bytes], *, executable: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for name, content in files.items():
            item = zipfile.ZipInfo("repo-commit/" + name)
            item.external_attr = (stat.S_IFREG | (0o755 if name == executable else 0o644)) << 16
            if name.endswith("/"):
                item.external_attr = (stat.S_IFDIR | 0o755) << 16
            output.writestr(item, content)
    return buffer.getvalue()


def setup_installer(
    tmp_path: Path,
    payload: bytes,
    *,
    user: bool = False,
) -> tuple[SkillInstaller, TaskWorkspace, str, list[str]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    user_root = tmp_path / "user-skills" if user else None
    task_workspace = TaskWorkspace(
        workspace, ArtifactStore(tmp_path / "artifacts"), repository, user_skills=user_root
    )
    task_workspace.bind_session(session.id)
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if "/commits/" in request.url.path:
            return httpx.Response(200, json={"sha": SHA})
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"default_branch": "release"})
        return httpx.Response(200, content=payload)

    installer = SkillInstaller(
        workspace,
        task_workspace,
        project_trusted=True,
        user_skills=user_root,
        refresh=lambda: None,
        transport=httpx.MockTransport(respond),
        host_guard=lambda _: True,
    )
    return installer, task_workspace, session.id, requests


async def test_repo_default_branch_complete_binary_install_idempotency_and_restore(tmp_path: Path) -> None:
    files = {
        "skills/sample/SKILL.md": SKILL,
        "skills/sample/references/help.md": b"help\r\n",
        "skills/sample/assets/icon.png": b"\x89PNG\x00\xff",
        "skills/sample/scripts/run.sh": b"#!/bin/sh\necho NEVER_EXECUTE\n",
        "skills/sample/empty/": b"",
        "README.md": b"repository readme",
    }
    installer, workspace, session_id, requests = setup_installer(
        tmp_path,
        archive(files, executable="skills/sample/scripts/run.sh"),
    )
    result = await installer.install_skill("owner/repo")
    assert result["status"] == "installed"
    target = Path(result["path"])
    assert (target / "SKILL.md").read_bytes() == SKILL
    assert (target / "assets/icon.png").read_bytes() == b"\x89PNG\x00\xff"
    assert (target / "references/help.md").read_bytes() == b"help\r\n"
    assert (target / "scripts/run.sh").stat().st_mode & 0o111
    assert (target / "empty").is_dir()
    assert not (target / "README.md").exists()
    assert requests[1].endswith("/commits/release")
    assert requests[2].endswith("/zip/" + SHA)
    assert len(json.dumps(result)) < 1500
    assert not workspace.completion_blockers(session_id)
    assert any(skill.name == "sample-skill" for skill in SkillLoader(installer.workspace.root).discover())
    repeated = await installer.install_skill("owner/repo")
    assert repeated["status"] == "already_installed"
    assert len(workspace.state_for(session_id).effects) == 1
    workspace.restore_work_product(result["work_product_id"], "missing")
    assert not await asyncio.to_thread(target.exists)
    assert not workspace.completion_blockers(session_id)


async def test_ambiguous_collection_returns_paths_without_writing(tmp_path: Path) -> None:
    installer, workspace, session_id, _ = setup_installer(
        tmp_path,
        archive(
            {
                "skills/one/SKILL.md": SKILL,
                "skills/two/SKILL.md": SKILL.replace(b"sample-skill", b"second-skill"),
            }
        ),
    )
    result = await installer.install_skill("https://github.com/owner/repo")
    assert result["status"] == "selection_required"
    assert len(result["candidates"]) == 2
    assert not (installer.workspace.root / ".lumen/skills").exists()
    assert not workspace.state_for(session_id).effects
    selected = await installer.install_skill("owner/repo", path="skills/two", ref=SHA)
    assert selected["name"] == "second-skill"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo/tree/main/skills/sample",
        "https://github.com/owner/repo/blob/main/skills/sample/SKILL.md",
        "https://raw.githubusercontent.com/owner/repo/main/skills/sample/SKILL.md",
    ],
)
async def test_explicit_skill_urls(tmp_path: Path, url: str) -> None:
    installer, _, _, requests = setup_installer(tmp_path, archive({"skills/sample/SKILL.md": SKILL}))
    result = await installer.install_skill(url)
    assert result["status"] == "installed"
    assert len(requests) == 2
    assert requests[0].endswith("/commits/main")


async def test_local_update_conflict_and_user_edits_are_preserved(tmp_path: Path) -> None:
    installer, workspace, session_id, _ = setup_installer(tmp_path, b"")
    source = installer.workspace.root / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(SKILL)
    first = await installer.install_skill("./source")
    target = Path(first["path"])
    (source / "SKILL.md").write_bytes(SKILL + b"upstream change\n")
    assert (await installer.install_skill("./source"))["status"] == "conflict"
    assert (target / "SKILL.md").read_bytes() == SKILL
    updated = await installer.install_skill("./source", overwrite=True)
    assert updated["status"] == "updated"
    (target / "SKILL.md").write_bytes(SKILL + b"user edit\n")
    (source / "SKILL.md").write_bytes(SKILL + b"next upstream change\n")
    with pytest.raises(ValueError, match="local changes"):
        await installer.install_skill("./source", overwrite=True)
    assert (target / "SKILL.md").read_bytes().endswith(b"user edit\n")
    assert not workspace.completion_blockers(session_id)


async def test_managed_user_scope_requires_parent_permission(tmp_path: Path) -> None:
    installer, _, _, requests = setup_installer(tmp_path, archive({"SKILL.md": SKILL}))
    denied = await installer.install_skill("owner/repo", scope="user")
    assert denied["status"] == "scope_unavailable"
    assert denied["available_scopes"] == ["project"]
    assert not requests
    other = tmp_path / "allowed"
    other.mkdir()
    installer, workspace, session_id, _ = setup_installer(other, archive({"SKILL.md": SKILL}), user=True)
    result = await installer.install_skill("owner/repo", scope="user")
    assert Path(result["path"]).parent == other / "user-skills"
    workspace.bind_session(session_id)
    assert not workspace.completion_blockers(session_id)
    workspace.restore_work_product(result["work_product_id"], "missing")
    assert not await asyncio.to_thread(Path(result["path"]).exists)


async def test_opt_in_global_install_preserves_command_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lumen.tools.workspace import Workspace, WorkspaceViolation

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "project"
    project.mkdir()
    config = project / "agent.yaml"
    config.write_text(
        "version: 2\nagent: {model: {id: test}, user_skill_install_enabled: true}\n"
        "tools: {builtins: [install_skill]}\nsandbox: {mode: workspace_write}\n"
    )
    manager = ResourceManager(load_config(config), workspace=project)
    session = manager.session_repository.create(agent_name="test", model_id="test")
    manager.bind_session_context(session.id)
    source = project / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(SKILL)
    approvals: list[ApprovalRequest] = []

    async def approve(request: ApprovalRequest) -> CapabilityApproval:
        approvals.append(request)
        return CapabilityApproval(approved=True)

    result = await manager.capability_gateway.invoke(CapabilityInvocation(
        execution_id="global", provider_call_id="global", name="install_skill",
        arguments={"source": "./source", "scope": "user"},
    ), approve=approve)
    assert result.succeeded, result.error
    assert len(approvals) == 1
    assert approvals[0].args["scope"] == "user"
    assert (tmp_path / "home/.lumen/skills/sample-skill/SKILL.md").read_bytes() == SKILL
    assert not (project / ".lumen/skills/sample-skill").exists()
    assert manager.config.sandbox.mode == "workspace_write"
    with pytest.raises(WorkspaceViolation):
        Workspace(project).resolve_for_mutation("../home/.lumen/skills/sample-skill")
    assert not manager.completion_issues(session.id)


@pytest.mark.parametrize("bad_path", ["../escape", "/escape", "a/../../escape", "a\\escape", "A/../b"])
async def test_archive_escape_is_rejected_before_publication(tmp_path: Path, bad_path: str) -> None:
    installer, workspace, session_id, _ = setup_installer(tmp_path, archive({bad_path: SKILL}))
    with pytest.raises(ValueError):
        await installer.install_skill("owner/repo")
    assert not workspace.state_for(session_id).effects
    assert not (installer.workspace.root / ".lumen/skills").exists()


async def test_symlink_archive_is_rejected(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        output.writestr("repo/SKILL.md", SKILL)
        link = zipfile.ZipInfo("repo/reference")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "../../secret")
    installer, _, _, _ = setup_installer(tmp_path, buffer.getvalue())
    with pytest.raises(ValueError, match="symbolic link"):
        await installer.install_skill("owner/repo")


async def test_failed_directory_swap_restores_previous_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer, workspace, session_id, _ = setup_installer(tmp_path, b"")
    source = installer.workspace.root / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(SKILL)
    first = await installer.install_skill("./source")
    (source / "SKILL.md").write_bytes(SKILL + b"new")
    original_rename = Path.rename

    def fail_publish(path: Path, target: str | Path) -> Path:
        if path.name == "incoming":
            raise OSError("simulated disk failure")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises(OSError, match="disk failure"):
        await installer.install_skill("./source", overwrite=True)
    assert (Path(first["path"]) / "SKILL.md").read_bytes() == SKILL
    workspace.bind_session(session_id)
    assert not workspace.completion_blockers(session_id)


async def test_installer_gateway_and_catalog_refresh_from_empty_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    config = tmp_path / "agent.yaml"
    config.write_text("version: 2\nagent: {model: {id: test}}\ntools: {builtins: [install_skill]}\n")
    manager = ResourceManager(load_config(config), workspace=tmp_path)
    session = manager.session_repository.create(agent_name="test", model_id="test")
    manager.bind_session_context(session.id)
    assert not manager.skills
    assert "load_skill" in manager.registry.entries
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(SKILL)
    calls = 0

    async def approve(_: ApprovalRequest) -> CapabilityApproval:
        nonlocal calls
        calls += 1
        return CapabilityApproval(approved=True)

    invocation = CapabilityInvocation(
        execution_id="test",
        provider_call_id="install",
        name="install_skill",
        arguments={"source": "./source"},
    )
    denied = await manager.capability_gateway.invoke(
        invocation.model_copy(update={"provider_call_id": "denied"}),
    )
    assert not denied.succeeded
    assert not (tmp_path / ".lumen/skills").exists()
    assert not manager.task_workspace.state_for(session.id).effects
    installed = await manager.capability_gateway.invoke(invocation, approve=approve)
    assert installed.succeeded, installed.error
    assert calls == 1
    assert "sample-skill" in manager.skill_catalog()
    loaded = await manager.capability_gateway.invoke(
        CapabilityInvocation(
            execution_id="test",
            provider_call_id="load",
            name="load_skill",
            arguments={"name": "sample-skill"},
        )
    )
    assert loaded.succeeded, loaded.error
    assert "Use references/help.md" in (loaded.model_output or "")
    old = manager.session_context.resolve_documents(session.id).skill_documents
    (tmp_path / ".lumen/skills/sample-skill/SKILL.md").write_bytes(SKILL + b"changed")
    manager.list_skills()
    assert manager.session_context.resolve_documents(session.id).skill_documents == old
    assert not manager.completion_issues(session.id)


async def test_model_install_then_load_in_one_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    config = tmp_path / "agent.yaml"
    config.write_text("version: 2\nagent: {model: {id: test}}\ntools: {builtins: [install_skill]}\n")
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(SKILL)
    calls = 0

    async def model_function(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="install_skill", json_args='{"source":"./source"}', tool_call_id="install"
                )
            }
            return
        if calls == 2:
            assert "sample-skill" in str(messages)
            assert "sample-skill" not in (info.instructions or "")
            yield {
                0: DeltaToolCall(name="load_skill", json_args='{"name":"sample-skill"}', tool_call_id="load")
            }
            return
        assert "Use references/help.md" in str(messages)
        yield "Installed and loaded sample-skill."

    model = FunctionModel(stream_function=model_function)

    def build_model(_: object) -> FunctionModel:
        return model

    monkeypatch.setattr("lumen.resources.build_model", build_model)
    manager = ResourceManager(load_config(config), workspace=tmp_path)
    session = manager.session_repository.create(agent_name="test", model_id="test")
    events: list[RunEvent] = []

    async def emit(event: RunEvent) -> None:
        events.append(event)

    async def approve(_: ApprovalRequest) -> ToolApproval:
        return ToolApproval(approved=True)

    async with manager:
        assert manager.runtime is not None
        outcome = await manager.runtime.run(
            "Install the skill in ./source and load it.",
            [],
            emit,
            approve,
            session_id=session.id,
        )
    assert outcome.output == "Installed and loaded sample-skill."
    assert calls == 3
    assert not manager.completion_issues(session.id)


async def test_network_cancellation_never_publishes(tmp_path: Path) -> None:
    installer, workspace, session_id, _ = setup_installer(tmp_path, b"")
    started = asyncio.Event()

    async def respond(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200)

    installer.transport = httpx.MockTransport(respond)
    task = asyncio.create_task(installer.install_skill("owner/repo"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workspace.state_for(session_id).effects
    assert not (installer.workspace.root / ".lumen/skills").exists()


async def test_slash_branch_and_invalid_sources(tmp_path: Path) -> None:
    installer, _, _, requests = setup_installer(tmp_path, archive({"skills/taste/SKILL.md": SKILL}))
    result = await installer.install_skill(
        "https://github.com/owner/repo/tree/feature/design/skills/taste",
        ref="feature/design",
    )
    assert result["status"] == "installed"
    assert "/commits/feature%2Fdesign" in requests[0]
    for source in (
        "https://user:secret@github.com/owner/repo",
        "http://github.com/a/b",
        "https://evil.test/a/b",
    ):
        with pytest.raises(ValueError):
            await installer.install_skill(source)


async def test_untrusted_project_is_rejected_before_network(tmp_path: Path) -> None:
    installer, _, _, requests = setup_installer(tmp_path, b"")
    installer.project_trusted = False
    with pytest.raises(ValueError, match="trusted project"):
        await installer.install_skill("owner/repo")
    assert not requests


async def test_private_token_never_follows_an_unapproved_redirect(tmp_path: Path) -> None:
    installer, workspace, session_id, _ = setup_installer(tmp_path, b"")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://outside.example/archive.zip"})

    installer.token = lambda: "test-only-token"
    installer.transport = httpx.MockTransport(respond)
    with pytest.raises(ValueError, match="outside the approved hosts") as error:
        await installer.install_skill("owner/repo")
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-only-token"
    assert "test-only-token" not in str(error.value)
    assert not workspace.state_for(session_id).effects


@pytest.mark.parametrize("phase", [EffectStatus.PREPARED, EffectStatus.APPLIED])
async def test_directory_install_recovers_an_interrupted_receipt(tmp_path: Path, phase: EffectStatus) -> None:
    installer, workspace, session_id, _ = setup_installer(tmp_path, archive({"SKILL.md": SKILL}))
    result = await installer.install_skill("owner/repo")
    state = workspace.state_for(session_id)
    # Simulate a journal that ended before publication/verification was recorded.
    interrupted = state.effects[0].model_copy(
        update={
            "status": phase,
            "verification": None,
            "after": None if phase is EffectStatus.PREPARED else state.effects[0].after,
        }
    )
    workspace.repository.append_work_state(session_id, state.upsert_effect(interrupted))
    resumed = TaskWorkspace(installer.workspace.root, workspace.artifacts, workspace.repository)
    resumed.bind_session(session_id)
    assert resumed.state_for(session_id).effects[0].status is EffectStatus.VERIFIED
    assert not resumed.completion_blockers(session_id)
    resumed.restore_work_product(result["work_product_id"], "missing")
    assert not await asyncio.to_thread(Path(result["path"]).exists)
