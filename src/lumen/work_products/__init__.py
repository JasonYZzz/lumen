"""Generic, session-scoped work products and verified effects."""

from .adapters import (
    AdapterError,
    ResourceAdapter,
    StaleResourceError,
    StructuredResourceAdapter,
    TextResourceAdapter,
)
from .types import (
    EffectReceipt,
    EffectStatus,
    RevisionSnapshot,
    SessionWorkState,
    TargetCandidate,
    VerificationResult,
    WorkProductEvent,
    WorkProductKind,
    WorkProductRef,
    WorkProductStatus,
)
from .workspace import TaskWorkspace

__all__ = [
    "AdapterError",
    "EffectReceipt",
    "EffectStatus",
    "ResourceAdapter",
    "RevisionSnapshot",
    "SessionWorkState",
    "StaleResourceError",
    "StructuredResourceAdapter",
    "TargetCandidate",
    "TaskWorkspace",
    "TextResourceAdapter",
    "VerificationResult",
    "WorkProductEvent",
    "WorkProductKind",
    "WorkProductRef",
    "WorkProductStatus",
]
