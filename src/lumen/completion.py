"""Shared completion gate for text, voice, and future interaction adapters."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from lumen.plan import EvidenceKind, PlanState


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
        external_issues: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._external_issues = external_issues

    def evaluate(
        self,
        *,
        session_id: str,
        plan: PlanState,
        policy: CompletionPolicy,
    ) -> list[str]:
        issues = list(self._external_issues(session_id)) if self._external_issues else []
        if policy.require_reviewable_plan and not plan.is_reviewable():
            issues.append("plan_invalid: planning requires a non-empty goal and all-pending steps")

        required = policy.required_revision
        if required is None and plan.approved_revision is None:
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


__all__ = ["CompletionGate", "CompletionPolicy", "CompletionSuggestion"]
