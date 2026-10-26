from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_automation import resources_for

from lumen.application import EditAssistant, WorkspaceHost
from lumen.automation.models import AssistantEdit, Goal, GoalExecution, Resource


@pytest.mark.parametrize("status", [None, "active", "review", "paused"])
async def test_ideas_follow_goal_state_and_keep_revisioned_context(
    tmp_path: Path, status: str | None,
) -> None:
    host = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await host.open()
    try:
        goal = Goal(id="goal", title="Ship report", intent="Keep source evidence", revision=3)
        if status:
            goal = goal.model_copy(update={"execution": GoalExecution.model_validate({
                "id": "activation", "criteria": "Evidence", "status": status,
            })})
        with host.automations.store.transaction() as db:
            host.automations.store._put(db, "goal", goal, goal.id)  # pyright: ignore[reportPrivateUsage]
        ideas = (await host.automations.state()).ideas
        contextual = [idea for idea in ideas if idea.source == "context"]
        if status == "active":
            assert contextual == []
        else:
            assert len(contextual) == 1
            idea = contextual[0]
            assert idea.outcome
            if status == "review":
                assert idea.destination == "goal" and idea.target_id == goal.id
            else:
                assert idea.destination == "task" and idea.steps
                assert idea.context_refs[0].id == goal.id and idea.context_refs[0].revision == 3
            await host.dispatch(EditAssistant(AssistantEdit(action="idea", id=idea.id, value=True)))
            updated = await host.automations.state()
            assert next(item for item in updated.ideas if item.id == idea.id).dismissed
        with host.automations.store.transaction() as db:
            host.automations.store._put(db, "goal", goal.model_copy(update={  # pyright: ignore[reportPrivateUsage]
                "deleted_at": datetime.now(UTC), "revision": 4,
            }), goal.id)
        assert all(idea.source != "context" for idea in (await host.automations.state()).ideas)
    finally:
        await host.close()


async def test_ideas_use_visible_document_results_and_leave_generic_starters_honest(tmp_path: Path) -> None:
    host = WorkspaceHost(resources_for(tmp_path))  # type: ignore[arg-type]
    await host.open()
    try:
        resource = Resource(id="doc", title="Project notes", artifact_ref="sha256:" + "a" * 64,
                            kind="file", file_type="document", revision=2, created_at=datetime.now(UTC))
        for identity, changes in (("doc", {}), ("image", {"file_type": "image"}),
                                  ("material", {"published": False}),
                                  ("deleted", {"deleted_at": datetime.now(UTC)})):
            host.automations.store.import_file(
                resource.model_copy(update={"id": identity, **changes}), text="",
            )
        state = await host.automations.state()
        contextual = [idea for idea in state.ideas if idea.source == "context"]
        assert len(contextual) == 1
        assert contextual[0].context_refs[0].id == "doc" and contextual[0].context_refs[0].revision == 2
        assert "不覆盖原资料" in contextual[0].intent
        starters = [idea for idea in state.ideas if idea.source == "template"]
        assert len(starters) == 3
        assert all(idea.input_hint and idea.outcome and idea.steps and not idea.context_refs
                   for idea in starters)
        assert all(idea.source_label == "通用起点" for idea in starters)
        assert not state.occurrences
    finally:
        await host.close()
