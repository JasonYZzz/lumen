"""Durable contracts for session-scoped work products and effects."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from lumen.tools.spec import EffectKind


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _WorkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkProductKind(StrEnum):
    TEXT = "text"
    JSON = "json"
    YAML = "yaml"


class WorkProductStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class EffectStatus(StrEnum):
    PREPARED = "prepared"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    NOT_APPLIED = "not_applied"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class RevisionSnapshot(_WorkModel):
    revision: str
    content_ref: str | None = None
    exists: bool = True
    byte_size: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=_now)


class TargetCandidate(_WorkModel):
    id: str
    selector: str
    label: str
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    json_pointer: str | None = None
    value: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


class VerificationResult(_WorkModel):
    passed: bool
    summary: str
    target_changed: bool = False
    non_target_unchanged: bool = False


class WorkProductRef(_WorkModel):
    id: str
    resource: str
    kind: WorkProductKind
    current: RevisionSnapshot
    status: WorkProductStatus = WorkProductStatus.ACTIVE
    targets: tuple[TargetCandidate, ...] = Field(default_factory=tuple)
    opened_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class EffectReceipt(_WorkModel):
    id: str
    effect_kind: EffectKind
    operation: str
    status: EffectStatus
    work_product_id: str | None = None
    resource: str | None = None
    target: TargetCandidate | None = None
    change_ref: str | None = None
    change_format: str | None = None
    before_value_summary: Any = None
    after_value_summary: Any = None
    before: RevisionSnapshot | None = None
    after: RevisionSnapshot | None = None
    verification: VerificationResult | None = None
    summary: str = ""
    error: str | None = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class SessionWorkState(_WorkModel):
    work_products: tuple[WorkProductRef, ...] = Field(default_factory=tuple)
    effects: tuple[EffectReceipt, ...] = Field(default_factory=tuple)

    def product(self, work_product_id: str) -> WorkProductRef | None:
        return next((item for item in self.work_products if item.id == work_product_id), None)

    def upsert_product(self, product: WorkProductRef) -> SessionWorkState:
        items = [item for item in self.work_products if item.id != product.id]
        items.append(product)
        return self.model_copy(update={"work_products": tuple(items)})

    def upsert_effect(self, effect: EffectReceipt) -> SessionWorkState:
        items = [item for item in self.effects if item.id != effect.id]
        items.append(effect)
        return self.model_copy(update={"effects": tuple(items)})


class WorkProductEvent(TypedDict):
    phase: str
    work_product_id: str | None
    resource: str | None
    effect_id: str | None
    status: str
    summary: str


__all__ = [
    "EffectReceipt",
    "EffectStatus",
    "RevisionSnapshot",
    "SessionWorkState",
    "TargetCandidate",
    "VerificationResult",
    "WorkProductEvent",
    "WorkProductKind",
    "WorkProductRef",
    "WorkProductStatus",
]
