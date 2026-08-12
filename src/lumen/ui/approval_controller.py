"""Approval flow control, extracted verbatim from ``app.py``.

``ApprovalControllerMixin`` owns the runtime approval callbacks (single and
batch), the pending-approval futures they await, and the panel/card decision
messages that resolve those futures. ``LumenApp`` is only imported under
``TYPE_CHECKING`` to avoid a circular import.
"""

# Textual discovers handlers through MessagePump's metaclass, while these
# cooperative mixins deliberately type ``self`` as the final LumenApp.
# Pyright cannot express that intersection type and otherwise reports every
# cross-mixin member as private plus an invalid explicit-self type.
# pyright: reportGeneralTypeIssues=false, reportPrivateUsage=false

from __future__ import annotations

import asyncio
from collections import Counter
from typing import TYPE_CHECKING, cast

from textual import on
from textual.message_pump import MessagePump
from textual.widgets import Static

from lumen.approval import ApprovalDecision, ApprovalMode, ApprovalPolicy
from lumen.collaboration import CollaborationMode
from lumen.events import ApprovalRequest, ToolApprovalBatchPending, ToolApprovalPending
from lumen.runtime import ToolApproval
from lumen.ui.approval_panel import ApprovalPanel
from lumen.ui.composer import PromptEditor, edit_text_external
from lumen.ui.plan_review_panel import PlanReviewPanel
from lumen.ui.tool_card import ToolCard

if TYPE_CHECKING:
    from lumen.ui.app import LumenApp


class ApprovalControllerMixin(MessagePump):
    """Bridge runtime approval requests to UI panels and back via futures.

    Inherits ``MessagePump`` so Textual's metaclass registers the ``@on``
    handlers below into this class's ``_decorated_handlers``; dispatch walks
    ``LumenApp``'s MRO and finds them here.
    """

    async def _await_inline_approval(self: LumenApp, request: ApprovalRequest) -> ToolApproval:
        """Allocate a future for ``request`` and await the card's decision.

        Policy-resolved decisions skip the card. This covers both approvals in
        accept-edits/auto and read-only denials in plan mode. Unknown remote
        capabilities remain approval-gated outside plan mode.
        """

        if (
            self._collaboration_mode is not CollaborationMode.PLAN
            and self._approval_scope_key(request) in self._session_approval_keys
        ):
            return ToolApproval(
                approved=True,
                message=(
                    "auto-approved by the user's session rule "
                    f"(mode={self._approval_mode.value}, decision_source=user_session)"
                ),
            )
        policy_decision = self._decide_for_modes(request)
        if not policy_decision.requires_confirmation:
            return ToolApproval(
                approved=policy_decision.approved,
                message=policy_decision.message,
            )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[ToolApproval] = loop.create_future()
        self._approval_waiters[request.call_id] = future
        # Surface the pending request to the user via the matching tool card.
        pending = ToolApprovalPending(
            call_id=request.call_id,
            name=request.name,
            args=request.args,
            origin=request.origin,
            risk=request.risk,
        )
        await self._render_event(pending)
        return await future

    async def _await_inline_approval_batch(
        self: LumenApp, requests: tuple[ApprovalRequest, ...]
    ) -> dict[str, ToolApproval]:
        results: dict[str, ToolApproval] = {}
        pending: list[ApprovalRequest] = []
        for request in requests:
            if (
                self._collaboration_mode is not CollaborationMode.PLAN
                and self._approval_scope_key(request) in self._session_approval_keys
            ):
                results[request.call_id] = ToolApproval(
                    True,
                    "auto-approved by the user's session rule "
                    f"(mode={self._approval_mode.value}, decision_source=user_session)",
                )
                continue
            policy_decision = self._decide_for_modes(request)
            if not policy_decision.requires_confirmation:
                results[request.call_id] = ToolApproval(policy_decision.approved, policy_decision.message)
                continue
            future: asyncio.Future[ToolApproval] = asyncio.get_running_loop().create_future()
            self._approval_waiters[request.call_id] = future
            pending.append(request)
        if len(pending) == 1:
            request = pending[0]
            await self._render_event(
                ToolApprovalPending(
                    request.call_id,
                    request.name,
                    request.args,
                    request.origin,
                    request.risk,
                )
            )
        elif pending:
            risks = Counter(request.risk for request in pending)
            summary = ", ".join(f"{count}x{risk}" for risk, count in sorted(risks.items()))
            await self._render_event(
                ToolApprovalBatchPending(
                    batch_id=f"batch-{pending[0].call_id}",
                    requests=tuple(pending),
                    risk_summary=summary,
                )
            )
        if pending:
            decisions = await asyncio.gather(
                *(self._approval_waiters[request.call_id] for request in pending)
            )
            results.update(
                (request.call_id, decision) for request, decision in zip(pending, decisions, strict=True)
            )
        return results

    @staticmethod
    def _approval_scope_key(request: ApprovalRequest | ToolApprovalPending) -> str:
        """Return the bounded capability remembered by an approval choice."""

        if request.name == "run_command":
            argv = request.args.get("argv")
            items = cast(list[object], argv) if isinstance(argv, list) else []
            executable = str(items[0]) if items else "<unknown>"
            return f"{request.origin}:{request.name}:{executable}"
        return f"{request.origin}:{request.name}"

    def _should_auto_approve(self: LumenApp, risk: str, *, name: str = "", origin: str = "builtin") -> bool:
        """Whether ``risk`` is auto-approved under the current mode.

        ``manual`` mode short-circuits reads and sends risky calls to the panel.
        ``auto`` mode short-circuits every known risk. The sole exception is
        ``external_unknown``, the safe default for an MCP tool the operator has
        not classified. This is the single choke point for local and MCP tools.
        """

        decision = self._decide_for_modes(
            ApprovalRequest(call_id="policy-check", name=name, args={}, origin=origin, risk=risk)
        )
        return decision.approved and not decision.requires_confirmation

    def _decide_for_modes(self: LumenApp, request: ApprovalRequest) -> ApprovalDecision:
        if self._collaboration_mode is CollaborationMode.PLAN:
            if ApprovalPolicy.is_read_only(request):
                return ApprovalDecision(
                    approved=True,
                    requires_confirmation=False,
                    source="collaboration_policy",
                    message="allowed in plan collaboration mode",
                )
            return ApprovalDecision(
                approved=False,
                requires_confirmation=False,
                source="collaboration_policy",
                message="blocked in plan collaboration mode",
            )
        return self._approval_policy.decide(request, self._approval_mode)

    def _resolve_all_pending_approvals(self: LumenApp, *, approved: bool, message: str) -> None:
        audit_message = f"{message} (mode={self._approval_mode.value}, decision_source=system)"
        for call_id, future in list(self._approval_waiters.items()):
            if not future.done():
                future.set_result(ToolApproval(approved=approved, message=audit_message))
            card = self._tool_cards.get(call_id)
            if card is not None:
                card.resolve_approval(approved=approved)
        self._approval_waiters.clear()
        try:
            self.query_one(ApprovalPanel).clear()
        except Exception:
            pass

    @on(ToolCard.Decision)
    def _handle_tool_decision(self: LumenApp, event: ToolCard.Decision) -> None:
        future = self._approval_waiters.pop(event.call_id, None)
        if future is None or future.done():
            return
        action = "allowed" if event.approved else "denied"
        message = (
            f"The user {action} this tool call (mode={self._approval_mode.value}, decision_source=user)."
        )
        future.set_result(ToolApproval(approved=event.approved, message=message))
        # Return focus to the prompt editor so the user can keep typing. The
        # tool card grabbed focus when its approval selector mounted; now that
        # the decision is resolved we hand it back.
        self.query_one("#prompt", PromptEditor).focus()

    @on(ApprovalPanel.Decision)
    def _handle_approval_decision(self: LumenApp, event: ApprovalPanel.Decision) -> None:
        panel = self.query_one(ApprovalPanel)
        request = panel.active_request
        future = self._approval_waiters.pop(event.call_id, None)
        panel.resolve(event.call_id)
        if future is None or future.done():
            return
        if event.remember and request is not None and request.call_id == event.call_id:
            self._session_approval_keys.add(self._approval_scope_key(request))
        action = "allowed" if event.approved else "denied"
        source = "user_session" if event.remember else "user"
        message = (
            f"The user {action} this tool call (mode={self._approval_mode.value}, decision_source={source})."
        )
        future.set_result(
            ToolApproval(approved=event.approved, message=message, remember=event.remember)
        )
        if panel.active_request is None:
            self.query_one("#prompt", PromptEditor).focus()

    @on(ApprovalPanel.BatchDecision)
    def _handle_approval_batch_decision(self: LumenApp, event: ApprovalPanel.BatchDecision) -> None:
        panel = self.query_one(ApprovalPanel)
        action = "allowed" if event.approved else "denied"
        message = (
            f"The user {action} this tool call "
            f"(mode={self._approval_mode.value}, decision_source=user_batch)."
        )
        for call_id in event.call_ids:
            future = self._approval_waiters.pop(call_id, None)
            panel.resolve(call_id)
            if future is not None and not future.done():
                future.set_result(ToolApproval(event.approved, message))
        self.query_one("#prompt", PromptEditor).focus()

    @on(PlanReviewPanel.Decision)
    async def _handle_plan_review_decision(self: LumenApp, event: PlanReviewPanel.Decision) -> None:
        panel = self.query_one(PlanReviewPanel)
        panel.hide()
        if event.mode is None:
            self.query_one("#status", Static).update(self._status("Plan ready for feedback"))
            editor = self.query_one("#prompt", PromptEditor)
            editor.placeholder = f"What should change in revision {self._plan_review_revision}?"
            editor.focus()
            return

        self._approval_mode = ApprovalMode.parse(event.mode)
        self._collaboration_mode = CollaborationMode.DEFAULT
        self.current_worker = self.run_worker(
            self._run_approved_plan(),
            name="approved-plan-run",
            exclusive=True,
        )

    @on(PlanReviewPanel.EditRequested)
    async def _handle_plan_edit_requested(self: LumenApp) -> None:
        """Edit the proposal externally, then send the result through RejectPlan/replan."""

        edited = await edit_text_external(self.plan.model_dump_json(indent=2), suffix=".json")
        if edited is None:
            self.notify("Set $VISUAL or $EDITOR to edit the plan", severity="warning")
            return
        if edited.strip() == self.plan.model_dump_json(indent=2).strip():
            self.notify("Plan unchanged", timeout=2)
            return
        self.query_one(PlanReviewPanel).hide()
        feedback = (
            "Revise the pending plan to match this user-edited proposal. Preserve valid IDs and "
            "acceptance criteria, and return a new revision for review:\n\n" + edited
        )
        self.current_worker = self.run_worker(
            self._run_rejected_plan(feedback), name="edited-plan-run", exclusive=True
        )
