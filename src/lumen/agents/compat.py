"""Deprecated Child Run Interface backed by the native AgentOrchestrator."""

from __future__ import annotations

import json

from lumen.child_runs import ChildKind, ChildRunRecord, ChildStatus

from .orchestrator import AgentOrchestrator
from .types import AgentStatus, AgentThreadState

_STATUS_MAP = {
    AgentStatus.QUEUED: ChildStatus.QUEUED,
    AgentStatus.RUNNING: ChildStatus.RUNNING,
    AgentStatus.WAITING: ChildStatus.RUNNING,
    AgentStatus.APPROVAL_PENDING: ChildStatus.RUNNING,
    AgentStatus.COMPLETED: ChildStatus.COMPLETED,
    AgentStatus.FAILED: ChildStatus.FAILED,
    AgentStatus.INTERRUPTED: ChildStatus.CANCELLED,
    AgentStatus.RECONCILIATION_REQUIRED: ChildStatus.FAILED,
    AgentStatus.IMPORT_PENDING: ChildStatus.IMPORT_PENDING,
    AgentStatus.IMPORTED: ChildStatus.IMPORTED,
    AgentStatus.REJECTED: ChildStatus.REJECTED,
    AgentStatus.CLOSED: ChildStatus.CLOSED,
    AgentStatus.NOT_CARRIED: ChildStatus.FAILED,
}


class LegacyChildRunAdapter:
    """Compatibility Adapter; new code should use AgentOrchestrator directly."""

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self.orchestrator = orchestrator

    async def spawn_child(
        self,
        task: str,
        kind: ChildKind = ChildKind.RESEARCH,
        plan_step_id: str | None = None,
    ) -> str:
        agent_type = "explorer" if ChildKind(kind) is ChildKind.RESEARCH else "worker"
        payload = json.loads(
            await self.orchestrator.spawn_agent(
                task,
                agent_type=agent_type,
                plan_step_id=plan_step_id,
            )
        )
        return json.dumps(
            {
                "child_id": payload["ref"]["id"],
                "status": payload["status"],
                "kind": kind.value,
            }
        )

    async def wait_children(self, child_ids: list[str], timeout_seconds: float = 30.0) -> str:
        await self.orchestrator.wait_agent(child_ids, int(timeout_seconds * 1000))
        return json.dumps(
            [
                self._record(self.orchestrator.get_agent(item)).model_dump(mode="json")
                for item in child_ids
            ],
            ensure_ascii=False,
        )

    async def cancel_child(self, child_id: str) -> str:
        await self.orchestrator.interrupt_agent(child_id)
        return self._record(self.orchestrator.get_agent(child_id)).model_dump_json()

    def list(self, session_id: str) -> list[ChildRunRecord]:
        return [self._record(item) for item in self.orchestrator.list(session_id)]

    async def approve_import(self, child_id: str) -> ChildRunRecord:
        return self._record(await self.orchestrator.approve_import(child_id))

    async def reject_import(self, child_id: str) -> ChildRunRecord:
        return self._record(await self.orchestrator.reject_import(child_id))

    async def close_child(self, child_id: str, *, preserve_status: bool = False) -> ChildRunRecord:
        thread = self.orchestrator.get_agent(child_id)
        if preserve_status:
            return self._record(thread)
        return self._record(
            AgentThreadState.model_validate_json(
                await self.orchestrator.close_agent(
                    child_id,
                    resolution="legacy_close" if thread.status in {
                        AgentStatus.FAILED,
                        AgentStatus.INTERRUPTED,
                        AgentStatus.RECONCILIATION_REQUIRED,
                    } else None,
                    reason="closed through legacy child interface" if thread.status in {
                        AgentStatus.FAILED,
                        AgentStatus.INTERRUPTED,
                        AgentStatus.RECONCILIATION_REQUIRED,
                    } else None,
                )
            )
        )

    @staticmethod
    def _record(thread: AgentThreadState) -> ChildRunRecord:
        return ChildRunRecord(
            id=thread.ref.id,
            parent_session_id=thread.ref.parent_session_id,
            plan_step_id=thread.plan_step_id,
            kind=(
                ChildKind.RESEARCH
                if thread.config.workspace_mode.value == "read-only"
                else ChildKind.WORKTREE
            ),
            task=thread.task,
            status=_STATUS_MAP[thread.status],
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            result=thread.result.summary if thread.result else "",
            error=thread.result.error if thread.result else None,
            worktree=thread.worktree,
            branch=thread.branch,
            commit=thread.commit,
            base_commit=thread.base_commit,
            diff=thread.diff_artifact_ref or "",
            evidence=list(thread.result.evidence) if thread.result else [],
        )


__all__ = ["LegacyChildRunAdapter"]
