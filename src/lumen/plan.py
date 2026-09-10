"""Structured, reviewable plan artifacts shared by every client.

Plan definitions and execution state intentionally use separate counters:
``revision`` changes only when the reviewed definition changes, while
``state_version`` changes for progress, ownership, and evidence updates.
"""

# Model-facing Chinese prose is kept as authored for readability.
# ruff: noqa: RUF001

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class PlanLifecycle(StrEnum):
    DRAFT = "draft"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EvidenceKind(StrEnum):
    TOOL = "tool"
    COMMAND = "command"
    DIFF = "diff"
    AGENT = "agent"
    CHILD = "child"
    USER_WAIVER = "user_waiver"


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    description: str = Field(min_length=1, max_length=300)


class PlanStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    depends_on: list[str] = Field(default_factory=list[str])
    acceptance_criteria: list[AcceptanceCriterion] = Field(
        default_factory=list[AcceptanceCriterion],
        description=(
            "由具体工具结果验证的可选要求。普通进度步骤和仅作推理准备的步骤（例如确定提纲）"
            "应留空。每项验收标准都必须在步骤完成前关联一条通过的执行回执。"
        ),
    )


class PlanStep(PlanStepInput):
    status: StepStatus = StepStatus.PENDING
    owner: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list[str])


class EvidenceReceipt(BaseModel):
    """Executor-created evidence; model tools may link but never create it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_ID_PATTERN)
    kind: EvidenceKind
    source_id: str = Field(min_length=1, max_length=200)
    criterion_ids: list[str] = Field(default_factory=list[str])
    summary: str = Field(min_length=1, max_length=1000)
    passed: bool
    sequence: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class PlanState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(default="", max_length=1000)
    constraints: list[str] = Field(default_factory=list[str])
    revision: int = Field(default=0, ge=0)
    state_version: int = Field(default=0, ge=0)
    lifecycle: PlanLifecycle = PlanLifecycle.DRAFT
    approved_revision: int | None = Field(default=None, ge=0)
    steps: list[PlanStep] = Field(default_factory=list[PlanStep])
    evidence: list[EvidenceReceipt] = Field(default_factory=list[EvidenceReceipt])

    @model_validator(mode="after")
    def validate_graph_and_references(self) -> PlanState:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("duplicate plan step ids are not allowed")
        known = set(step_ids)
        criterion_ids: set[str] = set()
        for step in self.steps:
            missing = sorted(set(step.depends_on) - known)
            if missing:
                raise ValueError(f"step {step.id!r} has unknown dependencies: {missing}")
            if step.id in step.depends_on:
                raise ValueError(f"step {step.id!r} cannot depend on itself")
            for criterion in step.acceptance_criteria:
                if criterion.id in criterion_ids:
                    raise ValueError(f"duplicate acceptance criterion id: {criterion.id}")
                criterion_ids.add(criterion.id)
        visiting: set[str] = set()
        visited: set[str] = set()
        edges = {step.id: step.depends_on for step in self.steps}

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError(f"plan dependency cycle includes {step_id!r}")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in edges[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)
        evidence_ids = [receipt.id for receipt in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence receipt ids must be unique")
        evidence_known = set(evidence_ids)
        for step in self.steps:
            missing_evidence = sorted(set(step.evidence_ids) - evidence_known)
            if missing_evidence:
                raise ValueError(f"step {step.id!r} has unknown evidence: {missing_evidence}")
        if self.approved_revision is not None and self.approved_revision > self.revision:
            raise ValueError("approved_revision cannot exceed revision")
        return self

    def completion_issues(self, *, require_approved_revision: bool = False) -> list[str]:
        issues: list[str] = []
        if require_approved_revision and self.approved_revision != self.revision:
            issues.append("plan revision is not approved")
        receipts = {receipt.id: receipt for receipt in self.evidence}
        for step in self.steps:
            if step.status not in {StepStatus.COMPLETED, StepStatus.SKIPPED}:
                issues.append(f"step {step.id} is {step.status.value}")
                continue
            if step.status is StepStatus.SKIPPED:
                continue
            linked = [receipts[item] for item in step.evidence_ids if item in receipts]
            covered = {
                criterion_id
                for receipt in linked
                if receipt.passed
                for criterion_id in receipt.criterion_ids
            }
            for criterion in step.acceptance_criteria:
                if criterion.id not in covered:
                    issues.append(f"criterion {criterion.id} has no passing evidence")
        return issues

    def is_reviewable(self) -> bool:
        return bool(self.goal.strip() and self.steps) and all(
            step.status is StepStatus.PENDING for step in self.steps
        )
