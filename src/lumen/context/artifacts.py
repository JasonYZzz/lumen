"""Content-addressed artifact store for bulky tool outputs (M3).

When a tool returns a large body (a 50 MB build log, a big file read), keeping
it verbatim in active history crowds the window for the whole session. The
store writes the body to a content-addressed file and returns a receipt
(:class:`ToolReceipt`) that keeps just enough head/tail/summary for the model
to reason without the full body (plan §9.3).

Security (plan §19):

* Artifact files are created with mode 0600 and the store directory 0700.
* Refuses to read or write paths that escape the store root via ``..`` or
  symlinks (resolved path must stay under the root).
* A tool may declare ``artifact_policy="never"`` (secret-bearing outputs) so
  the body is never written to disk; only a redacted receipt is kept.
* Cleanup is refcount-based: an artifact is removed only when its last holder
  releases it, never by broad glob. Holds are tracked per holder id (session id,
  checkpoint id) so a crash-and-replay can re-add the same hold idempotently.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Literal

#: Outputs at or above this many bytes are spilled to an artifact instead of
#: inlined in the receipt (plan §9.3 threshold). Tunable via the store.
DEFAULT_INLINE_THRESHOLD_BYTES = 4_096

#: Default head/tail sizes kept in the receipt so the model has context without
#: re-loading the artifact.
DEFAULT_RECEIPT_HEAD_CHARS = 1_200
DEFAULT_RECEIPT_TAIL_CHARS = 1_200

ArtifactPolicy = Literal["auto", "never"]


class ArtifactStoreError(RuntimeError):
    """Raised when an artifact operation would violate the store's invariants."""


class ArtifactStore:
    """Content-addressed store for tool-output bodies (plan §9.3).

    The store is keyed by ``sha256:<hex>``. ``store`` is idempotent: writing the
    same content twice returns the same ref and does not rewrite the file.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        inline_threshold_bytes: int = DEFAULT_INLINE_THRESHOLD_BYTES,
        receipt_head_chars: int = DEFAULT_RECEIPT_HEAD_CHARS,
        receipt_tail_chars: int = DEFAULT_RECEIPT_TAIL_CHARS,
    ) -> None:
        if inline_threshold_bytes < 0:
            raise ValueError("inline_threshold_bytes must be non-negative")
        self.root = Path(root).expanduser().resolve()
        self.inline_threshold_bytes = inline_threshold_bytes
        self.receipt_head_chars = receipt_head_chars
        self.receipt_tail_chars = receipt_tail_chars
        # 0700 dir; created lazily so a store for a session that never artifacts
        # touches nothing on disk.
        self._holds: dict[str, set[str]] = {}

    # -- storage ----------------------------------------------------------

    def store(self, content: bytes | str) -> str:
        """Write ``content`` to the store and return its ``sha256:<hex>`` ref.

        Idempotent: a second store of the same content is a no-op. The file is
        created with 0600 and never overwrites an existing artifact.
        """

        body = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(body).hexdigest()
        ref = f"sha256:{digest}"
        path = self._artifact_path(ref)
        if not path.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            # Write to a temp file in the same dir, then atomically rename with
            # the correct mode. Avoids a partial artifact visible to readers.
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(body)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        return ref

    def read(self, ref: str) -> bytes | None:
        """Return the artifact body for ``ref``, or ``None`` if it is absent."""

        path = self._artifact_path(ref)
        if not path.is_file():
            return None
        return path.read_bytes()

    def _artifact_path(self, ref: str) -> Path:
        """Resolve ``ref`` to a path, refusing traversal out of the store root.

        The ref must be ``sha256:<64 hex>``; the resolved path must stay under
        the store root so a crafted ref cannot escape (plan §19).
        """

        if not ref.startswith("sha256:") or len(ref) != len("sha256:") + 64:
            raise ArtifactStoreError(f"invalid artifact ref: {ref!r}")
        hexpart = ref[len("sha256:") :]
        if not all(c in "0123456789abcdef" for c in hexpart):
            raise ArtifactStoreError(f"invalid artifact ref: {ref!r}")
        path = (self.root / hexpart).resolve()
        if path != self.root / hexpart and not path.is_relative_to(self.root):
            raise ArtifactStoreError(f"artifact ref escapes store root: {ref!r}")
        if not path.is_relative_to(self.root):
            raise ArtifactStoreError(f"artifact ref escapes store root: {ref!r}")
        return path

    # -- refcount --------------------------------------------------------

    def add_hold(self, ref: str, holder: str) -> None:
        """Record that ``holder`` references ``ref`` (idempotent per holder)."""

        self._holds.setdefault(ref, set()).add(holder)

    def release_hold(self, ref: str, holder: str) -> bool:
        """Release ``holder``'s hold on ``ref``; remove the artifact at zero refs.

        Returns True if the artifact file was removed. A hold that was never
        added is a no-op (crash-and-replay safety).
        """

        holders = self._holds.get(ref)
        if holders is None:
            return False
        holders.discard(holder)
        if holders:
            return False
        self._holds.pop(ref, None)
        path = self._artifact_path(ref)
        if path.is_file():
            path.unlink()
            return True
        return False

    def hold_count(self, ref: str) -> int:
        return len(self._holds.get(ref, set()))

    def spill(self, content: bytes | str, *, artifact_policy: ArtifactPolicy = "auto") -> str | None:
        """Store ``content`` when it is large and policy allows; return the ref.

        Returns ``None`` when the body is small enough to inline or when the
        policy is ``never`` (secret-bearing: never persisted). The caller keeps
        head/tail in the receipt either way.
        """

        if artifact_policy == "never":
            return None
        body = content.encode("utf-8") if isinstance(content, str) else content
        if len(body) >= self.inline_threshold_bytes:
            return self.store(body)
        return None

    # -- receipt ---------------------------------------------------------

    def build_receipt(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        content: bytes | str,
        status: Literal["success", "error", "cancelled"],
        summary: str,
        source_event_ids: tuple[str, ...] = (),
        artifact_policy: ArtifactPolicy = "auto",
    ) -> tuple[str, str | None]:
        """Return ``(receipt_text, artifact_ref)`` for a tool output.

        When the body is large enough (and policy is ``auto``), it is spilled
        to the store and ``artifact_ref`` is set; the receipt text carries only
        head/tail. When policy is ``never`` (secret-bearing) the body is never
        written and ``artifact_ref`` is ``None`` regardless of size (plan §9.3).
        """

        body = content.encode("utf-8") if isinstance(content, str) else content
        artifact_ref: str | None = None
        if artifact_policy == "never":
            # Redacted: never persist the body; the receipt summary is all the
            # model sees. The sha256 is still computed so /context can show the
            # digest without the body being recoverable from disk.
            digest = hashlib.sha256(body).hexdigest()
            head = body[: self.receipt_head_chars].decode("utf-8", errors="replace")
            tail = body[-self.receipt_tail_chars :].decode("utf-8", errors="replace")
            return (
                _receipt_text(
                    tool_name=tool_name,
                    status=status,
                    summary=summary,
                    head=head,
                    tail=tail,
                    byte_size=len(body),
                    sha256=digest,
                    artifact_ref=None,
                    source_event_ids=source_event_ids,
                    redacted=True,
                ),
                None,
            )
        if len(body) >= self.inline_threshold_bytes:
            artifact_ref = self.store(body)
        head = body[: self.receipt_head_chars].decode("utf-8", errors="replace")
        tail = (
            body[-self.receipt_tail_chars :].decode("utf-8", errors="replace")
            if len(body) > self.receipt_head_chars
            else ""
        )
        digest = artifact_ref.split(":", 1)[1] if artifact_ref else hashlib.sha256(body).hexdigest()
        return (
            _receipt_text(
                tool_name=tool_name,
                status=status,
                summary=summary,
                head=head,
                tail=tail,
                byte_size=len(body),
                sha256=digest,
                artifact_ref=artifact_ref,
                source_event_ids=source_event_ids,
                redacted=False,
            ),
            artifact_ref,
        )


def _receipt_text(
    *,
    tool_name: str,
    status: str,
    summary: str,
    head: str,
    tail: str,
    byte_size: int,
    sha256: str,
    artifact_ref: str | None,
    source_event_ids: tuple[str, ...],
    redacted: bool,
) -> str:
    """Render a compact, model-visible receipt block for a tool output."""

    lines = [
        f"[tool-receipt {tool_name} status={status} bytes={byte_size} sha256={sha256[:12]}]",
        f"summary: {summary}",
    ]
    if redacted:
        lines.append("content: <redacted: artifact_policy=never>")
    elif artifact_ref is not None:
        lines.append(f"artifact: {artifact_ref}")
        if head:
            lines.append(f"head: {head}")
        if tail:
            lines.append(f"tail: {tail}")
    else:
        lines.append(f"content: {head}")
    if source_event_ids:
        lines.append(f"source_events: {','.join(source_event_ids)}")
    lines.append("[/tool-receipt]")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_INLINE_THRESHOLD_BYTES",
    "DEFAULT_RECEIPT_HEAD_CHARS",
    "DEFAULT_RECEIPT_TAIL_CHARS",
    "ArtifactPolicy",
    "ArtifactStore",
    "ArtifactStoreError",
]
