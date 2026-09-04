from __future__ import annotations

from pathlib import Path

from lumen.context.artifacts import ArtifactStore
from lumen.context.session_manager import SessionContextManager
from lumen.sessions import SessionRepository
from lumen.skills import Skill


def _skill(root: Path, body: str) -> Skill:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return Skill(
        name="review",
        description="review changes",
        file_path=path,
        base_dir=root,
        body=body,
        source="project",
    )


async def test_context_sources_are_session_scoped_and_resume_exact_artifacts(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session_a = repository.create(agent_name="test", model_id="test")
    session_b = repository.create(agent_name="test", model_id="test")
    current_skill = _skill(tmp_path / "skill", "revision one")

    async def fetch(reference: str) -> dict[str, object]:
        return {"server": "docs", "uri": reference, "body": "remote snapshot", "revision": "e1"}

    manager = SessionContextManager(
        repository=repository,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        skill_loader=lambda name: current_skill if name == "review" else None,
        resource_loader=fetch,
    )
    manager.activate_skill(session_a.id, "review")
    await manager.activate_resource(session_a.id, "docs://guide")

    assert manager.resolve_documents(session_b.id).skill_documents == ()
    assert manager.resolve_documents(session_b.id).resource_documents == ()

    # Reconstruct both manager and catalog with a changed source. Resume must
    # still resolve the exact activated artifact revision.
    changed_skill = _skill(tmp_path / "skill", "revision two")
    resumed = SessionContextManager(
        repository=SessionRepository(tmp_path / "sessions"),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        skill_loader=lambda name: changed_skill if name == "review" else None,
        resource_loader=fetch,
    )
    resolved = resumed.resolve_documents(session_a.id)
    assert resolved.skill_documents[0]["body"] == "revision one"
    assert resolved.resource_documents[0]["body"] == "remote snapshot"
    assert str(resolved.skill_documents[0]["body_artifact_ref"]).startswith("sha256:")
    assert str(resolved.resource_documents[0]["body_artifact_ref"]).startswith("sha256:")

    resumed.deactivate(session_a.id, "skill", "review")
    assert resumed.resolve_documents(session_a.id).skill_documents == ()

    pending = resumed.new_clarification(
        session_a.id,
        question="Choose a target",
        choices=("A", "B"),
        related_plan_step="inspect",
    )
    restarted = SessionContextManager(
        repository=SessionRepository(tmp_path / "sessions"),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        skill_loader=lambda _name: None,
        resource_loader=fetch,
    )
    assert restarted.load(session_a.id).pending_clarification == pending


def test_missing_artifact_is_diagnostic_and_not_injected(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    skill = _skill(tmp_path / "skill", "snapshot")
    artifacts = ArtifactStore(tmp_path / "artifacts")

    async def fetch(reference: str) -> dict[str, object]:
        return {"server": "docs", "uri": reference, "body": "unused"}

    manager = SessionContextManager(repository, artifacts, lambda _name: skill, fetch)
    state = manager.activate_skill(session.id, "review")
    artifact_path = artifacts.root / state.active_skills[0].body_artifact_ref.removeprefix("sha256:")
    artifact_path.unlink()

    resolved = manager.resolve_documents(session.id)
    assert resolved.skill_documents == ()
    assert "unavailable" in resolved.diagnostics[0]


def test_v5_turn_pagination_skips_context_state_records(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    repository.append_context_state(session.id, repository.load(session.id).context_state)
    repository.append_turn(
        session.id,
        user_input="hello",
        messages=[],
        approvals=[],
        usage={},
        status="completed",
    )

    page = repository.load_turn_page(session.id)
    assert [turn.user_input for turn in page.turns] == ["hello"]


def test_artifact_gc_uses_durable_session_references(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    skill = _skill(tmp_path / "skill", "live snapshot")
    artifacts = ArtifactStore(tmp_path / "artifacts")

    async def fetch(reference: str) -> dict[str, object]:
        return {"server": "docs", "uri": reference, "body": "unused"}

    manager = SessionContextManager(repository, artifacts, lambda _name: skill, fetch)
    state = manager.activate_skill(session.id, "review")
    live_ref = state.active_skills[0].body_artifact_ref
    orphan_ref = artifacts.store("orphan snapshot")

    assert manager.garbage_collect_artifacts() == (orphan_ref,)
    assert artifacts.read(live_ref) == b"live snapshot"
    assert artifacts.read(orphan_ref) is None
