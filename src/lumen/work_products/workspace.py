"""Session-scoped work-product orchestration behind one small interface."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import uuid4

from lumen.context.artifacts import ArtifactStore
from lumen.tools.spec import EffectKind
from lumen.tools.workspace import Workspace

from .adapters import AdapterError, ResourceAdapter, StructuredResourceAdapter, TextResourceAdapter
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

T = TypeVar("T")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkStateRepository(Protocol):
    def load(self, session_id: str) -> Any: ...
    def append_work_state(self, session_id: str, state: SessionWorkState) -> None: ...
    def append_effect(self, session_id: str, effect: EffectReceipt) -> None: ...


class TaskWorkspace:
    """Deep module that owns target location, journaling, verification and recovery.

    Callers bind a session and use the four model-facing operations. Adapters,
    artifact snapshots, transaction ordering and reconciliation remain private
    implementation details.
    """

    def __init__(
        self,
        root: str | Path,
        artifacts: ArtifactStore,
        repository: WorkStateRepository,
        *,
        enabled: bool = True,
        auto_attach: bool = True,
        strict: bool = True,
        max_context_items: int = 8,
    ) -> None:
        self.workspace = Workspace(root)
        self.artifacts = artifacts
        self.repository = repository
        self.enabled = enabled
        self.auto_attach = auto_attach
        self.strict = strict
        self.max_context_items = max_context_items
        self._session_id: ContextVar[str | None] = ContextVar(
            "lumen_task_workspace_session_id",
            default=None,
        )
        self._states: dict[str, SessionWorkState] = {}
        self._pending_events: dict[str, list[WorkProductEvent]] = {}
        self._adapters: dict[WorkProductKind, ResourceAdapter] = {
            WorkProductKind.TEXT: TextResourceAdapter(self.workspace, artifacts),
            WorkProductKind.JSON: StructuredResourceAdapter(
                self.workspace, artifacts, WorkProductKind.JSON
            ),
            WorkProductKind.YAML: StructuredResourceAdapter(
                self.workspace, artifacts, WorkProductKind.YAML
            ),
        }

    def bind_session(self, session_id: str) -> None:
        self._session_id.set(session_id)
        self._states[session_id] = self.repository.load(session_id).work_state
        self.reconcile(session_id)

    def open_work_product(
        self,
        resource: str,
        kind: str | None = None,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Open a workspace resource and optionally locate a target within it."""

        self._require_enabled()
        resource = self._canonical_resource(resource)
        session_id, state = self._bound()
        resolved_kind = self._resolve_kind(resource, kind)
        adapter = self._adapters[resolved_kind]
        snapshot = adapter.snapshot(resource)
        targets = adapter.locate(resource, snapshot, selector) if snapshot.exists else ()
        existing = next(
            (
                item
                for item in state.work_products
                if item.resource == resource and item.kind == resolved_kind
            ),
            None,
        )
        product = WorkProductRef(
            id=existing.id if existing is not None else f"work:{uuid4()}",
            resource=resource,
            kind=resolved_kind,
            current=snapshot,
            status=WorkProductStatus.ACTIVE if snapshot.exists else WorkProductStatus.MISSING,
            targets=targets,
            opened_at=existing.opened_at if existing is not None else _now(),
            updated_at=_now(),
        )
        state = state.upsert_product(product)
        self._save_state(session_id, state)
        self._queue_event(
            session_id,
            phase="opened",
            work_product_id=product.id,
            resource=product.resource,
            effect_id=None,
            status=product.status.value,
            summary=f"opened {product.kind.value} work product",
        )
        return self._product_payload(product)

    def inspect_work_product(
        self,
        work_product_id: str,
        selector: str | None = None,
    ) -> dict[str, Any]:
        """Refresh a product and return bounded target candidates."""

        self._require_enabled()
        session_id, state = self._bound()
        product = self._require_product(state, work_product_id)
        adapter = self._adapters[product.kind]
        actual = adapter.snapshot(product.resource)
        if actual.revision != product.current.revision:
            product = product.model_copy(
                update={"status": WorkProductStatus.RECONCILIATION_REQUIRED, "updated_at": _now()}
            )
            self._save_state(session_id, state.upsert_product(product))
            raise AdapterError(
                f"work product changed outside TaskWorkspace: {product.resource}; reopen it before editing"
            )
        targets = adapter.locate(product.resource, actual, selector)
        product = product.model_copy(update={"targets": targets, "updated_at": _now()})
        self._save_state(session_id, state.upsert_product(product))
        return self._product_payload(product)

    def change_work_product(
        self,
        work_product_id: str,
        target: str,
        change: Any,
    ) -> dict[str, Any]:
        """Apply and verify one constrained change to an opened product."""

        self._require_enabled()
        session_id, state = self._bound()
        product = self._require_product(state, work_product_id)
        adapter = self._adapters[product.kind]
        actual = adapter.snapshot(product.resource)
        if actual.revision != product.current.revision:
            self._mark_product_reconciliation(session_id, state, product)
            raise AdapterError(
                f"work product changed outside TaskWorkspace: {product.resource}; reopen it before editing"
            )
        candidates = self._target_candidates(adapter, product, actual, target)
        if len(candidates) != 1:
            return {
                "status": "target_not_unique",
                "selector": target,
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "summary": f"target selector matched {len(candidates)} candidates; no change applied",
            }
        candidate = candidates[0]
        change_ref, change_format = self._store_change(change, product.kind)
        structured = product.kind in {WorkProductKind.JSON, WorkProductKind.YAML}
        effect = EffectReceipt(
            id=f"effect:{uuid4()}",
            effect_kind=EffectKind.MUTATION,
            operation="change_work_product",
            status=EffectStatus.PREPARED,
            work_product_id=product.id,
            resource=product.resource,
            target=candidate,
            change_ref=change_ref,
            change_format=change_format,
            before_value_summary=candidate.value if structured else None,
            after_value_summary=self._bounded_summary(change) if structured else None,
            before=actual,
            summary=f"prepared isolated change for {candidate.label}",
        )
        self._save_effect(session_id, effect)
        try:
            after = adapter.apply(product.resource, actual, candidate, change)
            effect = effect.model_copy(
                update={
                    "status": EffectStatus.APPLIED,
                    "after": after,
                    "summary": f"applied isolated change for {candidate.label}",
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, effect)
            product = product.model_copy(
                update={
                    "current": after,
                    "targets": (),
                    "status": WorkProductStatus.ACTIVE,
                    "updated_at": _now(),
                }
            )
            current = self._state(session_id).upsert_product(product)
            self._save_state(session_id, current)
            verification = adapter.verify(product.resource, actual, after, candidate, change)
            effect = effect.model_copy(
                update={
                    "status": EffectStatus.VERIFIED if verification.passed else EffectStatus.FAILED,
                    "verification": verification,
                    "summary": verification.summary,
                    "error": None if verification.passed else verification.summary,
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, effect)
        except BaseException as error:
            self._record_failed_operation(
                session_id,
                effect,
                product,
                error,
                "work-product mutation failed",
            )
            raise
        return self._effect_payload(effect)

    def restore_work_product(
        self,
        work_product_id: str,
        revision: str | None = None,
    ) -> dict[str, Any]:
        """Restore an exact recorded revision without overwriting outside edits."""

        self._require_enabled()
        session_id, state = self._bound()
        product = self._require_product(state, work_product_id)
        adapter = self._adapters[product.kind]
        actual = adapter.snapshot(product.resource)
        if actual.revision != product.current.revision:
            self._mark_product_reconciliation(session_id, state, product)
            raise AdapterError("resource changed outside TaskWorkspace; refusing to restore over it")
        target_snapshot = self._find_restore_snapshot(state, product, revision)
        effect = EffectReceipt(
            id=f"effect:{uuid4()}",
            effect_kind=EffectKind.MUTATION,
            operation="restore_work_product",
            status=EffectStatus.PREPARED,
            work_product_id=product.id,
            resource=product.resource,
            before=actual,
            after=target_snapshot,
            summary=f"prepared restore to {target_snapshot.revision}",
        )
        self._save_effect(session_id, effect)
        try:
            restored = adapter.restore(
                product.resource,
                target_snapshot,
                expected_revision=actual.revision,
            )
            effect = effect.model_copy(
                update={
                    "status": EffectStatus.APPLIED,
                    "after": restored,
                    "summary": f"applied restore to {target_snapshot.revision}",
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, effect)
            passed = restored.revision == target_snapshot.revision
            verification = VerificationResult(
                passed=passed,
                target_changed=actual.revision != restored.revision,
                non_target_unchanged=True,
                summary=(
                    "exact revision restored"
                    if passed
                    else "restored revision does not match snapshot"
                ),
            )
            effect = effect.model_copy(
                update={
                    "status": EffectStatus.ROLLED_BACK if passed else EffectStatus.FAILED,
                    "after": restored,
                    "verification": verification,
                    "summary": verification.summary,
                    "error": None if passed else verification.summary,
                    "updated_at": _now(),
                }
            )
            product = product.model_copy(
                update={
                    "current": restored,
                    "targets": (),
                    "status": WorkProductStatus.ACTIVE if restored.exists else WorkProductStatus.MISSING,
                    "updated_at": _now(),
                }
            )
            self._save_state(session_id, self._state(session_id).upsert_product(product))
            self._save_effect(session_id, effect)
        except BaseException as error:
            self._record_failed_operation(
                session_id,
                effect,
                product,
                error,
                "restore failed",
            )
            raise
        return self._effect_payload(effect)

    def perform_text_mutation(
        self,
        resource: str,
        *,
        operation: str,
        selector: str,
        change: str,
        action: Callable[[], T],
    ) -> T:
        """Run a legacy file mutation while adding snapshots and verification."""

        if not self.enabled or not self.auto_attach or self._session_id.get() is None:
            return action()
        resource = self._canonical_resource(resource)
        session_id, state = self._bound()
        existing = next(
            (
                item
                for item in state.work_products
                if item.resource == resource and item.kind is WorkProductKind.TEXT
            ),
            None,
        )
        if existing is None:
            self.open_work_product(resource, WorkProductKind.TEXT.value)
            state = self._state(session_id)
            existing = next(
                item
                for item in state.work_products
                if item.resource == resource and item.kind is WorkProductKind.TEXT
            )
        adapter = self._adapters[existing.kind]
        before = adapter.snapshot(resource)
        candidates = adapter.locate(resource, before, selector)
        if len(candidates) != 1:
            raise AdapterError(f"selector matched {len(candidates)} targets; refusing legacy mutation")
        candidate = candidates[0]
        change_ref, change_format = self._store_change(change, WorkProductKind.TEXT)
        effect = EffectReceipt(
            id=f"effect:{uuid4()}",
            effect_kind=EffectKind.MUTATION,
            operation=operation,
            status=EffectStatus.PREPARED,
            work_product_id=existing.id,
            resource=resource,
            target=candidate,
            change_ref=change_ref,
            change_format=change_format,
            before=before,
            summary=f"prepared {operation}",
        )
        self._save_effect(session_id, effect)
        try:
            result = action()
            after = adapter.snapshot(resource)
            effect = effect.model_copy(
                update={
                    "status": EffectStatus.APPLIED,
                    "after": after,
                    "summary": f"applied {operation}",
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, effect)
            product = existing.model_copy(
                update={
                    "current": after,
                    "targets": (),
                    "status": WorkProductStatus.ACTIVE,
                    "updated_at": _now(),
                }
            )
            self._save_state(session_id, self._state(session_id).upsert_product(product))
            verification = adapter.verify(resource, before, after, candidate, change)
            effect = effect.model_copy(
                update={
                    "status": EffectStatus.VERIFIED if verification.passed else EffectStatus.FAILED,
                    "after": after,
                    "verification": verification,
                    "summary": verification.summary,
                    "error": None if verification.passed else verification.summary,
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, effect)
            if not verification.passed:
                raise AdapterError(verification.summary)
            return result
        except BaseException as error:
            if effect.status in {EffectStatus.PREPARED, EffectStatus.APPLIED}:
                self._record_failed_operation(
                    session_id,
                    effect,
                    existing,
                    error,
                    f"{operation} failed",
                )
            raise

    def record_tool_effect(
        self,
        *,
        tool_name: str,
        effect_kind: EffectKind,
        success: bool,
        summary: str,
        resource: str | None = None,
    ) -> EffectReceipt | None:
        """Record non-adapter tool effects without inventing resource snapshots."""

        if not self.enabled or self._session_id.get() is None or effect_kind is EffectKind.OBSERVE:
            return None
        session_id, _state = self._bound()
        if effect_kind in {EffectKind.EXECUTION, EffectKind.EXTERNAL_ACTION}:
            status = EffectStatus.VERIFIED if success else EffectStatus.FAILED
        elif effect_kind is EffectKind.UNKNOWN and success and self.strict:
            status = EffectStatus.RECONCILIATION_REQUIRED
        else:
            status = EffectStatus.VERIFIED if success else EffectStatus.FAILED
        effect = EffectReceipt(
            id=f"effect:{uuid4()}",
            effect_kind=effect_kind,
            operation=tool_name,
            status=status,
            resource=resource,
            summary=summary,
            error=None if success else summary,
        )
        self._save_effect(session_id, effect)
        return effect

    def prepare_import_effects(
        self,
        resources: tuple[str, ...],
        *,
        summary: str,
    ) -> tuple[str, ...]:
        """Journal file mutations immediately before an isolated Agent import.

        Git owns conflict detection and atomic application; TaskWorkspace owns
        durable before/after snapshots and completion verification. Unsupported
        resource kinds fail before the parent workspace is mutated.
        """

        if not self.enabled:
            return ()
        session_id, state = self._bound()
        effect_ids: list[str] = []
        for raw_resource in resources:
            resource = self._canonical_resource(raw_resource)
            kind = self._resolve_kind(resource, None)
            adapter = self._adapters[kind]
            before = adapter.snapshot(resource)
            existing = next(
                (item for item in state.work_products if item.resource == resource),
                None,
            )
            product = WorkProductRef(
                id=existing.id if existing is not None else f"work:{uuid4()}",
                resource=resource,
                kind=kind,
                current=before,
                status=(
                    WorkProductStatus.ACTIVE
                    if before.exists
                    else WorkProductStatus.MISSING
                ),
                opened_at=existing.opened_at if existing is not None else _now(),
                updated_at=_now(),
            )
            state = state.upsert_product(product)
            effect = EffectReceipt(
                id=f"effect:{uuid4()}",
                effect_kind=EffectKind.MUTATION,
                operation="import_agent_changes",
                status=EffectStatus.PREPARED,
                work_product_id=product.id,
                resource=resource,
                before=before,
                summary=summary,
            )
            self._save_state(session_id, state)
            self._save_effect(session_id, effect)
            state = self._state(session_id)
            effect_ids.append(effect.id)
            self._queue_event(
                session_id,
                phase="prepared",
                work_product_id=product.id,
                resource=resource,
                effect_id=effect.id,
                status=effect.status.value,
                summary=summary,
            )
        return tuple(effect_ids)

    def finalize_import_effects(
        self,
        effect_ids: tuple[str, ...],
        *,
        applied: bool,
        diff: str = "",
    ) -> bool:
        """Reconcile prepared Agent-import effects against actual file state."""

        if not effect_ids:
            return applied
        session_id, _state = self._bound()
        all_verified = applied
        change_ref = self.artifacts.store(diff) if diff else None
        for effect_id in effect_ids:
            state = self._state(session_id)
            effect = next((item for item in state.effects if item.id == effect_id), None)
            if effect is None or effect.resource is None or effect.before is None:
                all_verified = False
                continue
            product = state.product(effect.work_product_id or "")
            if product is None:
                all_verified = False
                continue
            adapter = self._adapters[product.kind]
            after = adapter.snapshot(effect.resource)
            changed = after.revision != effect.before.revision
            if applied:
                passed = changed
                status = EffectStatus.VERIFIED if passed else EffectStatus.FAILED
                summary = (
                    "verified isolated Agent import"
                    if passed
                    else "Agent import reported success but target did not change"
                )
                verification = VerificationResult(
                    passed=passed,
                    summary=summary,
                    target_changed=changed,
                    non_target_unchanged=True,
                )
                all_verified = all_verified and passed
            elif after.revision == effect.before.revision:
                status = EffectStatus.NOT_APPLIED
                summary = "Agent import was not applied"
                verification = VerificationResult(
                    passed=True,
                    summary=summary,
                    target_changed=False,
                    non_target_unchanged=True,
                )
            else:
                status = EffectStatus.RECONCILIATION_REQUIRED
                summary = "Agent import outcome could not be reconciled"
                verification = VerificationResult(
                    passed=False,
                    summary=summary,
                    target_changed=changed,
                    non_target_unchanged=False,
                )
                all_verified = False
            updated_effect = effect.model_copy(
                update={
                    "status": status,
                    "after": after,
                    "change_ref": change_ref,
                    "change_format": "git-diff" if change_ref else None,
                    "verification": verification,
                    "summary": summary,
                    "error": None if verification.passed else summary,
                    "updated_at": _now(),
                }
            )
            updated_product = product.model_copy(
                update={
                    "current": after,
                    "status": (
                        WorkProductStatus.ACTIVE
                        if after.exists
                        else WorkProductStatus.MISSING
                    ),
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, updated_effect)
            self._save_state(session_id, self._state(session_id).upsert_product(updated_product))
            self._queue_event(
                session_id,
                phase="verified" if verification.passed else "failed",
                work_product_id=product.id,
                resource=effect.resource,
                effect_id=effect.id,
                status=status.value,
                summary=summary,
            )
        return all_verified

    def reconcile(self, session_id: str | None = None) -> SessionWorkState:
        """Resolve interrupted journal entries against the current resource revision."""

        resolved = session_id or self._session_id.get()
        if resolved is None:
            return SessionWorkState()
        state = self._state(resolved)
        for effect in tuple(state.effects):
            if effect.status not in {EffectStatus.PREPARED, EffectStatus.APPLIED}:
                continue
            product = state.product(effect.work_product_id or "")
            if product is None or effect.resource is None:
                updated = self._reconciled_effect(effect, EffectStatus.RECONCILIATION_REQUIRED)
            else:
                adapter = self._adapters[product.kind]
                actual = adapter.snapshot(effect.resource)
                if effect.after is not None and actual.revision == effect.after.revision:
                    if effect.operation == "restore_work_product":
                        verification = VerificationResult(
                            passed=True,
                            target_changed=(
                                effect.before is not None
                                and effect.before.revision != actual.revision
                            ),
                            non_target_unchanged=True,
                            summary="resume reconciliation verified exact restored revision",
                        )
                        updated = effect.model_copy(
                            update={
                                "status": EffectStatus.ROLLED_BACK,
                                "verification": verification,
                                "summary": verification.summary,
                                "error": None,
                                "updated_at": _now(),
                            }
                        )
                    elif (
                        effect.before is not None
                        and effect.target is not None
                        and effect.change_ref is not None
                    ):
                        try:
                            change = self._load_change(effect)
                            verification = adapter.verify(
                                effect.resource,
                                effect.before,
                                actual,
                                effect.target,
                                change,
                            )
                        except (AdapterError, UnicodeDecodeError, json.JSONDecodeError) as error:
                            updated = effect.model_copy(
                                update={
                                    "status": EffectStatus.RECONCILIATION_REQUIRED,
                                    "summary": "resume verification artifact is unavailable",
                                    "error": str(error),
                                    "updated_at": _now(),
                                }
                            )
                        else:
                            updated = effect.model_copy(
                                update={
                                    "status": (
                                        EffectStatus.VERIFIED
                                        if verification.passed
                                        else EffectStatus.FAILED
                                    ),
                                    "verification": verification,
                                    "summary": verification.summary,
                                    "error": (
                                        None if verification.passed else verification.summary
                                    ),
                                    "updated_at": _now(),
                                }
                            )
                    else:
                        updated = self._reconciled_effect(
                            effect, EffectStatus.RECONCILIATION_REQUIRED
                        )
                    refreshed = product.model_copy(
                        update={
                            "current": actual,
                            "status": (
                                WorkProductStatus.ACTIVE
                                if actual.exists
                                else WorkProductStatus.MISSING
                            ),
                            "updated_at": _now(),
                        }
                    )
                    self._save_state(resolved, self._state(resolved).upsert_product(refreshed))
                elif effect.before is not None and actual.revision == effect.before.revision:
                    updated = self._reconciled_effect(effect, EffectStatus.NOT_APPLIED)
                elif (
                    effect.before is not None
                    and effect.target is not None
                    and effect.change_ref is not None
                ):
                    try:
                        change = self._load_change(effect)
                        verification = adapter.verify(
                            effect.resource,
                            effect.before,
                            actual,
                            effect.target,
                            change,
                        )
                    except (AdapterError, UnicodeDecodeError, json.JSONDecodeError) as error:
                        updated = effect.model_copy(
                            update={
                                "status": EffectStatus.RECONCILIATION_REQUIRED,
                                "after": actual,
                                "summary": "resume could not verify an interrupted apply",
                                "error": str(error),
                                "updated_at": _now(),
                            }
                        )
                    else:
                        updated = effect.model_copy(
                            update={
                                "status": (
                                    EffectStatus.VERIFIED
                                    if verification.passed
                                    else EffectStatus.RECONCILIATION_REQUIRED
                                ),
                                "after": actual,
                                "verification": verification,
                                "summary": verification.summary,
                                "error": None if verification.passed else verification.summary,
                                "updated_at": _now(),
                            }
                        )
                    refreshed = product.model_copy(
                        update={
                            "current": actual,
                            "status": (
                                WorkProductStatus.ACTIVE
                                if updated.status is EffectStatus.VERIFIED
                                else WorkProductStatus.RECONCILIATION_REQUIRED
                            ),
                            "updated_at": _now(),
                        }
                    )
                    self._save_state(resolved, self._state(resolved).upsert_product(refreshed))
                else:
                    updated = self._reconciled_effect(
                        effect, EffectStatus.RECONCILIATION_REQUIRED
                    )
            if updated != effect:
                self._save_effect(resolved, updated)
                state = self._state(resolved)
        return state

    def completion_issues(self, session_id: str | None = None) -> list[str]:
        if not self.enabled or not self.strict:
            return []
        resolved = session_id or self._session_id.get()
        if resolved is None:
            return []
        issues: list[str] = []
        state = self._state(resolved)
        for index, effect in enumerate(state.effects):
            if effect.status in {
                EffectStatus.PREPARED,
                EffectStatus.APPLIED,
                EffectStatus.RECONCILIATION_REQUIRED,
            }:
                issues.append(f"effect {effect.id} ({effect.operation}) is {effect.status.value}")
            elif (
                effect.status is EffectStatus.FAILED
                and effect.after is not None
                and not any(
                    later.work_product_id == effect.work_product_id
                    and later.status in {EffectStatus.VERIFIED, EffectStatus.ROLLED_BACK}
                    for later in state.effects[index + 1 :]
                )
            ):
                issues.append(f"effect {effect.id} ({effect.operation}) failed after mutation")
        for product in state.work_products:
            if product.status is WorkProductStatus.RECONCILIATION_REQUIRED:
                issues.append(f"work product {product.id} requires reconciliation")
        return issues

    def waive_effects(self, session_id: str, scope: tuple[str, ...], reason: str) -> int:
        """Resolve explicitly selected effects with a durable user waiver."""

        state = self._state(session_id)
        selected = set(scope)
        count = 0
        for effect in tuple(state.effects):
            if selected and effect.id not in selected:
                continue
            if effect.status not in {
                EffectStatus.PREPARED,
                EffectStatus.APPLIED,
                EffectStatus.FAILED,
                EffectStatus.RECONCILIATION_REQUIRED,
            }:
                continue
            waived = effect.model_copy(
                update={
                    "status": EffectStatus.VERIFIED,
                    "verification": VerificationResult(
                        passed=True,
                        summary=f"user verification waiver: {reason}",
                        target_changed=effect.after is not None,
                        non_target_unchanged=False,
                    ),
                    "summary": f"user verification waiver: {reason}",
                    "error": None,
                    "updated_at": _now(),
                }
            )
            self._save_effect(session_id, waived)
            if effect.work_product_id is not None:
                current_state = self._state(session_id)
                product = current_state.product(effect.work_product_id)
                if product is not None and product.status is WorkProductStatus.RECONCILIATION_REQUIRED:
                    actual = self._adapters[product.kind].snapshot(product.resource)
                    accepted = product.model_copy(
                        update={
                            "current": actual,
                            "status": (
                                WorkProductStatus.ACTIVE
                                if actual.exists
                                else WorkProductStatus.MISSING
                            ),
                            "updated_at": _now(),
                        }
                    )
                    self._save_state(session_id, self._state(session_id).upsert_product(accepted))
            count += 1
        return count

    def context_documents(self, session_id: str) -> tuple[dict[str, Any], ...]:
        if not self.enabled:
            return ()
        state = self._states.get(session_id)
        if state is None:
            state = self.repository.load(session_id).work_state
            self._states[session_id] = state
        products = list(state.work_products[-self.max_context_items :])
        effects = list(state.effects[-self.max_context_items :])
        if not products and not effects:
            return ()
        return (
            {
                "kind": "work_state",
                "work_products": [
                    {
                        "id": item.id,
                        "resource": item.resource,
                        "kind": item.kind.value,
                        "revision": item.current.revision,
                        "status": item.status.value,
                        "targets": [
                            {"id": target.id, "selector": target.selector, "label": target.label}
                            for target in item.targets
                        ],
                    }
                    for item in products
                ],
                "recent_effects": [
                    {
                        "id": item.id,
                        "operation": item.operation,
                        "effect_kind": item.effect_kind.value,
                        "status": item.status.value,
                        "resource": item.resource,
                        "summary": item.summary,
                    }
                    for item in effects
                ],
            },
        )

    def state_for(self, session_id: str) -> SessionWorkState:
        return self._states.get(session_id) or self.repository.load(session_id).work_state

    def drain_events(self, session_id: str) -> tuple[WorkProductEvent, ...]:
        pending = tuple(self._pending_events.get(session_id, ()))
        self._pending_events[session_id] = []
        return pending

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("work products are disabled")

    def _bound(self) -> tuple[str, SessionWorkState]:
        session_id = self._session_id.get()
        if session_id is None:
            raise RuntimeError("TaskWorkspace is not bound to a session")
        return session_id, self._state(session_id)

    def _state(self, session_id: str) -> SessionWorkState:
        state = self._states.get(session_id)
        if state is None:
            state = self.repository.load(session_id).work_state
            self._states[session_id] = state
        return state

    def _save_state(self, session_id: str, state: SessionWorkState) -> None:
        self._states[session_id] = state
        self.repository.append_work_state(session_id, state)

    def _save_effect(self, session_id: str, effect: EffectReceipt) -> None:
        state = self._state(session_id).upsert_effect(effect)
        self._states[session_id] = state
        self.repository.append_effect(session_id, effect)
        self._queue_event(
            session_id,
            phase=(
                "restored"
                if effect.status is EffectStatus.ROLLED_BACK
                else effect.status.value
            ),
            work_product_id=effect.work_product_id,
            resource=effect.resource,
            effect_id=effect.id,
            status=effect.status.value,
            summary=effect.summary,
        )

    def _queue_event(
        self,
        session_id: str,
        *,
        phase: str,
        work_product_id: str | None,
        resource: str | None,
        effect_id: str | None,
        status: str,
        summary: str,
    ) -> None:
        self._pending_events.setdefault(session_id, []).append(
            WorkProductEvent(
                phase=phase,
                work_product_id=work_product_id,
                resource=resource,
                effect_id=effect_id,
                status=status,
                summary=summary,
            )
        )

    def _require_product(self, state: SessionWorkState, work_product_id: str) -> WorkProductRef:
        product = state.product(work_product_id)
        if product is None:
            raise KeyError(f"work product not found: {work_product_id}")
        return product

    def _resolve_kind(self, resource: str, declared: str | None) -> WorkProductKind:
        if declared is not None:
            return WorkProductKind(declared.lower())
        suffix = Path(resource).suffix.lower()
        if suffix == ".json":
            return WorkProductKind.JSON
        if suffix in {".yaml", ".yml"}:
            return WorkProductKind.YAML
        return WorkProductKind.TEXT

    def _canonical_resource(self, resource: str) -> str:
        return self.workspace.resolve_for_mutation(resource).relative_to(self.workspace.root).as_posix()

    def _store_change(self, change: Any, kind: WorkProductKind) -> tuple[str, str]:
        if kind is WorkProductKind.TEXT and isinstance(change, str):
            return self.artifacts.store(change), "text"
        body = json.dumps(change, ensure_ascii=False, separators=(",", ":"))
        return self.artifacts.store(body), "json"

    def _load_change(self, effect: EffectReceipt) -> Any:
        if effect.change_ref is None:
            raise AdapterError(f"effect {effect.id} has no change artifact")
        body = self.artifacts.read(effect.change_ref)
        if body is None:
            raise AdapterError(f"change artifact is missing: {effect.change_ref}")
        text = body.decode("utf-8")
        return text if effect.change_format == "text" else json.loads(text)

    @staticmethod
    def _bounded_summary(value: Any, *, limit: int = 512) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str) and len(value) <= limit:
            return value
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) <= limit:
            return value
        return {
            "truncated": True,
            "type": type(value).__name__,
            "characters": len(encoded),
            "preview": encoded[:limit],
        }

    def _target_candidates(
        self,
        adapter: ResourceAdapter,
        product: WorkProductRef,
        snapshot: RevisionSnapshot,
        target: str,
    ) -> tuple[TargetCandidate, ...]:
        stored = next((item for item in product.targets if item.id == target), None)
        if stored is not None:
            return (stored,)
        return adapter.locate(product.resource, snapshot, target)

    def _find_restore_snapshot(
        self,
        state: SessionWorkState,
        product: WorkProductRef,
        revision: str | None,
    ) -> RevisionSnapshot:
        candidates: list[RevisionSnapshot] = [product.current]
        for effect in state.effects:
            if effect.work_product_id == product.id:
                if effect.before is not None:
                    candidates.append(effect.before)
                if effect.after is not None:
                    candidates.append(effect.after)
        if revision is not None:
            found = next((item for item in reversed(candidates) if item.revision == revision), None)
        else:
            found = next(
                (
                    effect.before
                    for effect in reversed(state.effects)
                    if effect.work_product_id == product.id and effect.before is not None
                ),
                None,
            )
        if found is None:
            raise KeyError(f"no recorded revision available for {product.id}")
        return found

    def _mark_product_reconciliation(
        self,
        session_id: str,
        state: SessionWorkState,
        product: WorkProductRef,
    ) -> None:
        updated = product.model_copy(
            update={"status": WorkProductStatus.RECONCILIATION_REQUIRED, "updated_at": _now()}
        )
        self._save_state(session_id, state.upsert_product(updated))

    def _record_failed_operation(
        self,
        session_id: str,
        effect: EffectReceipt,
        product: WorkProductRef,
        error: BaseException,
        summary: str,
    ) -> None:
        after = effect.after if effect.status is EffectStatus.APPLIED else None
        try:
            actual = self._adapters[product.kind].snapshot(product.resource)
        except BaseException:
            actual = None
        if actual is not None and effect.before is not None:
            if actual.revision != effect.before.revision:
                after = actual
                uncertain = product.model_copy(
                    update={
                        "current": actual,
                        "status": WorkProductStatus.RECONCILIATION_REQUIRED,
                        "updated_at": _now(),
                    }
                )
                self._save_state(
                    session_id,
                    self._state(session_id).upsert_product(uncertain),
                )
        failed = effect.model_copy(
            update={
                "status": EffectStatus.FAILED,
                "after": after,
                "error": str(error),
                "summary": summary,
                "updated_at": _now(),
            }
        )
        self._save_effect(session_id, failed)

    @staticmethod
    def _reconciled_effect(effect: EffectReceipt, status: EffectStatus) -> EffectReceipt:
        return effect.model_copy(
            update={
                "status": status,
                "summary": f"resume reconciliation: {status.value}",
                "updated_at": _now(),
            }
        )

    @staticmethod
    def _product_payload(product: WorkProductRef) -> dict[str, Any]:
        return product.model_dump(mode="json")

    @staticmethod
    def _effect_payload(effect: EffectReceipt) -> dict[str, Any]:
        return effect.model_dump(mode="json")


__all__ = ["TaskWorkspace", "WorkStateRepository"]
