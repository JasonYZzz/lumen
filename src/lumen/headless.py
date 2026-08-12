"""Single-shot, non-interactive agent runs behind ``lumen --print``.

The headless path reuses the same non-TUI runtime the Web client uses:
:class:`~lumen.application.WorkspaceHost` owns session state, event
journaling, approval policy, and JSONL persistence. This module only adds a
terminal consumer for the run's event stream: text deltas are streamed to
stdout in ``text`` mode, everything is collected into one JSON document in
``json`` mode, and approval requests that still require confirmation after
the policy ran are denied outright because no user is present to answer.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO
from uuid import uuid4

from lumen.application import (
    ApprovalStateError,
    CreateSession,
    DecideApproval,
    SetApprovalMode,
    SetCollaborationMode,
    StartRun,
    WorkspaceHost,
    WorkspaceResources,
)
from lumen.approval import ApprovalMode
from lumen.collaboration import CollaborationMode

_OUTPUT_FORMATS = frozenset({"text", "json"})


@dataclass(slots=True)
class HeadlessResult:
    """Terminal outcome of one headless run."""

    session_id: str = ""
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict[str, Any])
    request_count: int = 0
    tool_call_count: int = 0
    error: str | None = None

    def exit_code(self) -> int:
        """Process exit code: 0 for a completed run, 1 for any failure."""
        return 0 if self.error is None else 1

    def to_json_dict(self, *, model_id: str) -> dict[str, Any]:
        """Serialize for ``--output-format json`` script consumers."""
        return {
            "type": "result",
            "subtype": "error" if self.error is not None else "success",
            "is_error": self.error is not None,
            "result": self.text,
            "session_id": self.session_id,
            "model": model_id,
            "usage": self.usage,
            "num_turns": self.request_count,
            "tool_calls": self.tool_call_count,
            "error": self.error,
        }


async def run_headless(
    resources: WorkspaceResources,
    prompt: str,
    *,
    resume_id: str | None = None,
    permission_mode: ApprovalMode | None = None,
    collaboration_mode: CollaborationMode | None = None,
    output_format: str = "text",
    stdout: TextIO | None = None,
) -> HeadlessResult:
    """Run one agent turn without a TUI and render it for a pipe.

    Args:
        resources: Opened-lazily workspace resources (a ``ResourceManager``).
        prompt: User prompt for the single run.
        resume_id: Session UUID to continue instead of starting a new session.
        permission_mode: Override for the configured default approval mode;
            anything the policy still flags for confirmation is denied.
        output_format: ``text`` streams assistant deltas live; ``json`` emits
            one structured document after the run ends.
        stdout: Sink for rendered output; defaults to ``sys.stdout``.

    Returns:
        The terminal ``HeadlessResult``; check ``error``/``exit_code()``.
    """
    if output_format not in _OUTPUT_FORMATS:
        raise ValueError(f"output_format must be one of: {sorted(_OUTPUT_FORMATS)}")
    out = stdout if stdout is not None else sys.stdout
    result = HeadlessResult()
    streamed = False

    host = WorkspaceHost(resources)
    await host.open()
    try:
        if resume_id is not None:
            session_id = resume_id
        else:
            created = await host.dispatch(CreateSession())
            session_id = created.session_id
        if permission_mode is not None:
            await host.dispatch(SetApprovalMode(session_id, permission_mode.value))
        if collaboration_mode is not None:
            await host.dispatch(SetCollaborationMode(session_id, collaboration_mode.value))
        result.session_id = session_id
        started = await host.dispatch(StartRun(session_id, prompt, str(uuid4())))

        async for event in host.subscribe(started.run_id):
            if event.type == "assistant.delta":
                if output_format == "text":
                    streamed = True
                    out.write(str(event.data["text"]))
                    out.flush()
            elif event.type == "usage.updated":
                result.usage = dict(event.data.get("usage") or {})
                result.request_count = int(event.data.get("request_count", 0))
                result.tool_call_count = int(event.data.get("tool_call_count", 0))
            elif event.type == "run.completed":
                result.text = str(event.data.get("output", ""))
                result.usage = dict(event.data.get("usage") or result.usage)
            elif event.type == "run.waiting_for_user":
                # No one can answer a clarification headlessly; surface the
                # question as the result so the caller can re-prompt with it.
                result.text = str(event.data.get("question", ""))
            elif event.type == "run.failed":
                result.error = str(event.data.get("message", "run failed"))
            elif event.type == "run.cancelled":
                result.error = str(event.data.get("message", "run cancelled"))
            elif event.type == "approval.pending":
                await _deny(host, started.run_id, str(event.data["call_id"]))
            elif event.type == "approval.batch_pending":
                for request in event.data.get("requests", []):
                    await _deny(host, started.run_id, str(request["call_id"]))
    finally:
        await host.close()

    if output_format == "json":
        model_id = str(resources.active_model_config().id)
        out.write(json.dumps(result.to_json_dict(model_id=model_id), ensure_ascii=False) + "\n")
    else:
        if not streamed and result.text:
            out.write(result.text)
        if streamed or result.text:
            out.write("\n")
        out.flush()
    return result


async def _deny(host: WorkspaceHost, run_id: str, call_id: str) -> None:
    """Resolve one pending approval as denied; tolerates late races."""
    try:
        await host.dispatch(DecideApproval(run_id, call_id, False))
    except ApprovalStateError:
        # The run ended between the pending event and our decision; the host
        # already resolved the future with a denial, so nothing is left to do.
        pass


__all__ = ["HeadlessResult", "run_headless"]
