"""Tool-output reduction and receipts for active history (M3).

Replaces bulky tool outputs in the active history with compact
:class:`ToolReceipt` blocks so a 50 MB build log cannot crowd the window for
the whole session, while the full body remains recoverable from the
content-addressed artifact store (plan §9.2 steps 3-4, §9.3, §3.2 #8).

Reduction is deterministic and preserves tool pairing: only a
``ToolReturnPart``'s content is rewritten to a receipt; the matching
``ToolCallPart`` and its id are untouched, so ``validate_active_history`` still
holds. The most recent ``keep_recent_full`` messages are left verbatim so the
model still sees full tool outputs in the window compaction keeps.

The receipt keeps head + tail plus, for error outputs, the first line matching
an error pattern (extractive reduction, plan §9.2 step 4).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolReturnPart,
)

from lumen.context.artifacts import ArtifactPolicy, ArtifactStore
from lumen.context.types import ToolReceipt

#: Lines matching this (case-insensitive) are surfaced in an error receipt's
#: summary so the model sees why a tool failed without the full body.
_ERROR_LINE_PREFIXES = ("error", "fail", "traceback", "exception", "errno", "fatal")


@dataclass(frozen=True, slots=True)
class ReductionResult:
    """The reduced history plus the receipts produced for replaced outputs."""

    messages: list[ModelMessage]
    receipts: list[ToolReceipt]
    #: Number of tool outputs whose body was spilled to an artifact.
    artifacted: int
    #: Number of tool outputs replaced by a receipt (large, duplicate or empty).
    reduced: int


def _text(content: Any) -> bytes:
    """Render a ToolReturnPart content to bytes for hashing/sizing/receipt.

    Structured content (canonical dict/list tool outputs) is serialized as
    JSON rather than a Python ``repr`` so receipts stay deterministic and
    model-readable; anything else falls back to ``str``.
    """

    if isinstance(content, str):
        return content.encode("utf-8")
    if content is None:
        return b""
    if isinstance(content, (Mapping, list, tuple)):
        try:
            return json.dumps(content, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - defensive for exotic keys
            pass
    try:
        return str(cast(Any, content)).encode("utf-8")
    except Exception:  # pragma: no cover - defensive for exotic content
        return repr(cast(Any, content)).encode("utf-8")


def _first_error_line(body: bytes) -> str:
    """First line matching an error pattern, or "" if none (extractive)."""

    for line in body.decode("utf-8", errors="replace").splitlines():
        lowered = line.strip().lower()
        if any(lowered.startswith(prefix) for prefix in _ERROR_LINE_PREFIXES):
            return line.strip()[:200]
    return ""


def build_receipt(
    store: ArtifactStore,
    *,
    tool_call_id: str,
    tool_name: str,
    content: object,
    status: str,
    source_event_ids: tuple[str, ...] = (),
    artifact_policy: ArtifactPolicy = "auto",
    head_chars: int,
    tail_chars: int,
) -> tuple[ToolReceipt, str]:
    """Build a structured :class:`ToolReceipt` and its model-visible text.

    Spills the body to the store when it is large and the policy allows; the
    receipt keeps head/tail regardless. ``never`` policy redacts the body (never
    persisted) but still records the sha256 so /context can show a digest.
    """

    body = _text(content)
    artifact_ref = store.spill(body, artifact_policy=artifact_policy)
    redacted = artifact_policy == "never"
    # never policy: the summary must not echo the (secret) body, so it is
    # generic rather than extractive.
    summary = (
        f"{tool_name} {status} (redacted, {len(body)} bytes)"
        if redacted
        else _summary(tool_name, status, body)
    )
    # head/tail are also redacted for never policy: keep them empty so no body
    # fragment reaches the model. The sha256 is still recorded for /context.
    head = "" if redacted else body[:head_chars].decode("utf-8", errors="replace")
    tail = (
        ""
        if redacted
        else body[-tail_chars:].decode("utf-8", errors="replace")
        if len(body) > head_chars
        else ""
    )
    sha = hashlib.sha256(body).hexdigest()
    receipt = ToolReceipt(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        head=head,
        tail=tail,
        byte_size=len(body),
        sha256=sha,
        artifact_ref=artifact_ref,
        source_event_ids=source_event_ids,
    )
    return receipt, _render_receipt(receipt, redacted=redacted)


def _summary(tool_name: str, status: str, body: bytes) -> str:
    """A short status line: error line for failures, else size + first line."""

    line_count = body.count(b"\n") + (1 if body else 0)
    if status == "error":
        err = _first_error_line(body)
        if err:
            return f"{tool_name} failed: {err}"
        return f"{tool_name} failed ({len(body)} bytes)"
    if not body.strip():
        return f"{tool_name} returned empty output"
    first = body.decode("utf-8", errors="replace").strip().splitlines()[0][:160]
    return f"{tool_name} {status} ({len(body)} bytes, {line_count} lines): {first}"


def _render_receipt(receipt: ToolReceipt, *, redacted: bool) -> str:
    """Render the model-visible receipt block (kept compact)."""

    lines = [
        f"[tool-receipt {receipt.tool_name} status={receipt.status} "
        f"bytes={receipt.byte_size} sha256={receipt.sha256[:12]}]",
        f"summary: {receipt.summary}",
    ]
    if redacted:
        lines.append("content: <redacted: artifact_policy=never>")
    elif receipt.artifact_ref is not None:
        lines.append(f"artifact: {receipt.artifact_ref}")
        lines.append("search: search_artifacts(query=...) 可检索全部已转存输出的完整正文")
        if receipt.head:
            lines.append(f"head: {receipt.head}")
        if receipt.tail:
            lines.append(f"tail: {receipt.tail}")
    else:
        lines.append(f"content: {receipt.head}")
    lines.append("[/tool-receipt]")
    return "\n".join(lines)


def reduce_tool_outputs(
    history: Sequence[ModelMessage],
    store: ArtifactStore,
    *,
    keep_recent_full: int = 0,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> ReductionResult:
    """Reduce bulky/empty/duplicate tool outputs in ``history`` to receipts.

    ``keep_recent_full`` trailing messages are left verbatim so the recent
    window the model actually sees keeps full tool outputs; everything before
    is eligible for receipt-ization. Tool pairing is preserved (only the
    ``ToolReturnPart`` content changes). Returns the reduced messages and the
    structured receipts (for /context and artifact refcount tracking).
    """

    head = head_chars if head_chars is not None else store.receipt_head_chars
    tail = tail_chars if tail_chars is not None else store.receipt_tail_chars
    messages = list(history)
    cut = max(0, len(messages) - keep_recent_full)
    seen_digests: set[str] = set()
    receipts: list[ToolReceipt] = []
    artifacted = 0
    reduced = 0

    for index in range(cut):
        message = messages[index]
        replaced = _maybe_replace(message, store, seen_digests, head=head, tail=tail, receipts=receipts)
        if replaced is not None:
            if replaced.artifact_ref is not None:
                artifacted += 1
            reduced += 1
            messages[index] = replaced.message

    return ReductionResult(
        messages=messages,
        receipts=receipts,
        artifacted=artifacted,
        reduced=reduced,
    )


@dataclass(frozen=True, slots=True)
class _Replacement:
    message: ModelMessage
    artifact_ref: str | None


def _maybe_replace(
    message: ModelMessage,
    store: ArtifactStore,
    seen_digests: set[str],
    *,
    head: int,
    tail: int,
    receipts: list[ToolReceipt],
) -> _Replacement | None:
    """Receipt-ize one message's tool returns if large/empty/duplicate.

    Returns the rewritten message (with receipt content) or ``None`` when the
    message has no reducing tool return. A duplicate body is replaced with a
    ``duplicate`` receipt (content dropped) so repeats don't re-pay the cost.
    """

    parts = getattr(message, "parts", None)
    if not parts:
        return None
    tool_returns = [p for p in parts if isinstance(p, ToolReturnPart)]
    if not tool_returns:
        return None
    new_parts: list[Any] = []
    changed = False
    for part in parts:
        if not isinstance(part, ToolReturnPart):
            new_parts.append(part)
            continue
        body = _text(part.content)
        digest = hashlib.sha256(body).hexdigest()
        is_empty = not body.strip()
        is_duplicate = (not is_empty) and digest in seen_digests and len(body) > 0
        seen_digests.add(digest)
        # Only reduce when there's something to gain: empty, duplicate, or large.
        is_large = len(body) >= store.inline_threshold_bytes
        if not (is_empty or is_duplicate or is_large):
            new_parts.append(part)
            continue
        status = "error" if _looks_like_error(part) else "success"
        if is_duplicate:
            receipt, text = _duplicate_receipt(part, digest, status)
        else:
            receipt, text = build_receipt(
                store,
                tool_call_id=part.tool_call_id or "",
                tool_name=part.tool_name,
                content=part.content,
                status=status,
                head_chars=head,
                tail_chars=tail,
            )
        receipts.append(receipt)
        new_parts.append(
            ToolReturnPart(
                tool_name=part.tool_name,
                content=text,
                tool_call_id=part.tool_call_id,
            )
        )
        changed = True
    if not changed:
        return None
    new_message = _rebuild_message(message, new_parts)
    artifact_ref = next((r.artifact_ref for r in receipts if r.artifact_ref is not None), None)
    return _Replacement(message=new_message, artifact_ref=artifact_ref)


def _duplicate_receipt(part: ToolReturnPart, digest: str, status: str) -> tuple[ToolReceipt, str]:
    """A receipt for an output identical to an earlier one (content dropped)."""

    receipt = ToolReceipt(
        tool_call_id=part.tool_call_id or "",
        tool_name=part.tool_name,
        status=status,  # type: ignore[arg-type]
        summary=f"duplicate of earlier output (sha256={digest[:12]})",
        head="",
        tail="",
        byte_size=len(_text(part.content)),
        sha256=digest,
        artifact_ref=None,
        source_event_ids=(),
    )
    return receipt, _render_receipt(receipt, redacted=False)


def _looks_like_error(part: ToolReturnPart) -> bool:
    """Heuristic: a *text* tool return whose content carries an error marker.

    Structured (dict/list) content is a validated canonical success — marker
    substrings such as an ``"engine_errors"`` key must not reclassify it.
    """

    if not isinstance(part.content, str):
        return False
    text = part.content.lower()
    return any(marker in text for marker in _ERROR_LINE_PREFIXES)


def _rebuild_message(message: ModelMessage, new_parts: list[Any]) -> ModelMessage:
    """Rebuild a message with replaced parts (pairing preserved).

    Tool returns live in ``ModelRequest`` parts, so only that case is rebuilt;
    a ``ModelResponse`` is returned unchanged (its parts are calls/text, never
    returns).
    """

    if isinstance(message, ModelRequest):
        return ModelRequest(parts=new_parts)
    return message


__all__ = [
    "ReductionResult",
    "build_receipt",
    "reduce_tool_outputs",
]
