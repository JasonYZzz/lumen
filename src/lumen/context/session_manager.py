"""Deep module for session-scoped Skill, MCP resource, and clarification state."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from lumen.context.artifacts import ArtifactStore
from lumen.context.session_state import (
    ActiveResourceRef,
    ActiveSkillRef,
    PendingClarification,
    ResolvedSessionContext,
    SessionContextState,
)
from lumen.sessions import SessionRepository
from lumen.skills import Skill

SkillLoader = Callable[[str], Skill | None]
ResourceLoader = Callable[[str], Awaitable[dict[str, object]]]


@dataclass(slots=True)
class SessionContextManager:
    repository: SessionRepository
    artifacts: ArtifactStore
    skill_loader: SkillLoader
    resource_loader: ResourceLoader

    def load(self, session_id: str) -> SessionContextState:
        return self.repository.load(session_id).context_state

    def activate_skill(self, session_id: str, name: str) -> SessionContextState:
        skill = self.skill_loader(name)
        if skill is None:
            raise FileNotFoundError(f"skill not found: {name}")
        now = datetime.now(UTC)
        revision = hashlib.sha256(skill.body.encode("utf-8")).hexdigest()
        artifact_ref = self.artifacts.store(skill.body)
        current = self.load(session_id)
        active = [item for item in current.active_skills if item.name != name]
        active.append(
            ActiveSkillRef(
                name=name,
                revision=revision,
                source=str(skill.file_path),
                body_artifact_ref=artifact_ref,
                activated_at=now,
                last_used_at=now,
            )
        )
        state = current.model_copy(update={"active_skills": tuple(active)})
        self.repository.append_context_state(session_id, state)
        return state

    async def activate_resource(self, session_id: str, reference: str) -> SessionContextState:
        document = await self.resource_loader(reference)
        body = str(document.get("body", ""))
        artifact_ref = self.artifacts.store(body)
        revision = str(document.get("revision") or hashlib.sha256(body.encode("utf-8")).hexdigest())
        current = self.load(session_id)
        active = [item for item in current.active_resources if item.reference != reference]
        active.append(
            ActiveResourceRef(
                reference=reference,
                server=str(document.get("server", "unknown")),
                uri=str(document.get("uri", reference)),
                revision=revision,
                body_artifact_ref=artifact_ref,
                fetched_at=datetime.now(UTC),
            )
        )
        state = current.model_copy(update={"active_resources": tuple(active)})
        self.repository.append_context_state(session_id, state)
        return state

    def deactivate(self, session_id: str, source_kind: str, reference: str) -> SessionContextState:
        current = self.load(session_id)
        if source_kind == "skill":
            state = current.model_copy(
                update={"active_skills": tuple(x for x in current.active_skills if x.name != reference)}
            )
        elif source_kind == "resource":
            state = current.model_copy(
                update={
                    "active_resources": tuple(x for x in current.active_resources if x.reference != reference)
                }
            )
        else:
            raise ValueError(f"unknown context source kind: {source_kind}")
        # P0 deliberately does not release artifacts: persistent GC must scan
        # every session/checkpoint before deciding an object is unreferenced.
        self.repository.append_context_state(session_id, state)
        return state

    def set_clarification(
        self,
        session_id: str,
        request: PendingClarification | None,
    ) -> SessionContextState:
        current = self.load(session_id)
        state = current.model_copy(update={"pending_clarification": request})
        self.repository.append_context_state(session_id, state)
        return state

    def new_clarification(
        self,
        session_id: str,
        *,
        question: str,
        choices: tuple[str, ...] = (),
        related_plan_step: str | None = None,
    ) -> PendingClarification:
        request = PendingClarification(
            id=f"clarify-{uuid4().hex[:12]}",
            question=question,
            choices=choices,
            related_plan_step=related_plan_step,
            created_at=datetime.now(UTC),
        )
        self.set_clarification(session_id, request)
        return request

    def resolve_documents(self, session_id: str) -> ResolvedSessionContext:
        state = self.load(session_id)
        skill_documents: list[dict[str, object]] = []
        resource_documents: list[dict[str, object]] = []
        diagnostics: list[str] = []
        for item in state.active_skills:
            body = self.artifacts.read(item.body_artifact_ref)
            if body is None:
                diagnostics.append(
                    f"active skill {item.name!r} unavailable: artifact {item.body_artifact_ref} is missing"
                )
                continue
            skill_documents.append(
                {
                    "name": item.name,
                    "revision": item.revision,
                    "source": item.source,
                    "body_artifact_ref": item.body_artifact_ref,
                    "body": body.decode("utf-8", errors="replace"),
                }
            )
        for item in state.active_resources:
            body = self.artifacts.read(item.body_artifact_ref)
            if body is None:
                diagnostics.append(
                    "active MCP resource "
                    f"{item.reference!r} unavailable: artifact {item.body_artifact_ref} is missing"
                )
                continue
            resource_documents.append(
                {
                    "reference": item.reference,
                    "server": item.server,
                    "uri": item.uri,
                    "revision": item.revision,
                    "body_artifact_ref": item.body_artifact_ref,
                    "body": body.decode("utf-8", errors="replace"),
                }
            )
        return ResolvedSessionContext(
            skill_documents=tuple(skill_documents),
            resource_documents=tuple(resource_documents),
            diagnostics=tuple(diagnostics),
        )

    def garbage_collect_artifacts(self) -> tuple[str, ...]:
        """Run persistent mark-and-sweep across every session record."""

        return self.artifacts.mark_and_sweep(self.repository.artifact_references())


__all__ = ["SessionContextManager"]
