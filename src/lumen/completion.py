"""Shared completion gate for text, voice, and future interaction adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lumen.plan import EvidenceKind, PlanState, StepStatus


@dataclass(frozen=True, slots=True)
class CompletionBlocker:
    """An unmet invariant with an explicit owner for recovery."""

    message: str
    model_recoverable: bool = True

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class CompletionSuggestion:
    """One displayed completion and the text inserted when it is selected."""

    label: str
    insert: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionPolicy:
    """Host-selected terminal policy applied before user-visible completion."""

    require_post_mutation_verification: bool = True
    max_retries: int = 2
    required_revision: int | None = None
    require_reviewable_plan: bool = False


class CompletionGate:
    """Evaluate all completion invariants through one runtime-neutral seam."""

    def __init__(
        self,
        external_issues: Callable[[str], Sequence[str | CompletionBlocker]] | None = None,
    ) -> None:
        self._external_issues = external_issues

    def evaluate(
        self,
        *,
        session_id: str,
        plan: PlanState,
        policy: CompletionPolicy,
        plan_updated: bool = False,
    ) -> list[str]:
        return [str(issue) for issue in self.assess(
            session_id=session_id, plan=plan, policy=policy, plan_updated=plan_updated,
        )]

    def assess(
        self,
        *,
        session_id: str,
        plan: PlanState,
        policy: CompletionPolicy,
        plan_updated: bool = False,
    ) -> list[str | CompletionBlocker]:
        """Preserve recovery ownership until the Loop decides whether to retry."""

        issues: list[str | CompletionBlocker] = (
            list(self._external_issues(session_id)) if self._external_issues else []
        )
        if policy.require_reviewable_plan:
            if not plan.is_reviewable():
                issues.append("plan_invalid: planning requires a non-empty goal and all-pending steps")
            return issues

        required = policy.required_revision
        if required is None and plan.approved_revision is None:
            # Default-mode plans also need explicit progress reconciliation.
            # A plan inherited from an earlier task must not block unrelated Q&A.
            if plan_updated:
                issues.extend(
                    f"step {step.id} is {step.status.value}; use update_step to complete, block, or skip it"
                    for step in plan.steps
                    if step.status in {StepStatus.PENDING, StepStatus.IN_PROGRESS}
                )
            return issues
        if required is not None and (plan.revision != required or plan.approved_revision != required):
            issues.append(f"approved plan revision {required} was structurally changed")
        issues.extend(plan.completion_issues(require_approved_revision=True))
        if not policy.require_post_mutation_verification:
            return issues

        mutation_sequence = max(
            (
                receipt.sequence
                for receipt in plan.evidence
                if receipt.kind is EvidenceKind.DIFF and receipt.passed
            ),
            default=0,
        )
        if mutation_sequence and not any(
            receipt.passed
            and receipt.sequence > mutation_sequence
            and receipt.kind in {EvidenceKind.COMMAND, EvidenceKind.USER_WAIVER}
            for receipt in plan.evidence
        ):
            issues.append("no passing verification command or user waiver after the last mutation")
        return issues


__all__ = ["CompletionBlocker", "CompletionGate", "CompletionPolicy", "CompletionSuggestion"]
