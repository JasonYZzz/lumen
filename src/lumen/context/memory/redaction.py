"""Secret/PII redaction for memory candidates (M6).

Facts bound for durable memory are redacted before they are ever written: API
keys, bearer tokens, private keys, cookies and full environment-style
assignments are stripped, never paraphrased into a memory (plan §11.6). The
detector is a conservative regex pass - it errs toward redacting borderline
content - plus a length heuristic for long opaque tokens.

Redaction is a separate stage from extraction so a consolidator with no network
or shell still sees only safe text, and so the same pass protects the SQLite
body, the Markdown projection and any future telemetry.
"""

from __future__ import annotations

import re

#: Patterns whose match is always redacted. Kept deliberately broad: a false
#: positive (redacting a non-secret) only loses a memory; a false negative
#: (storing a secret) leaks a credential across sessions.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(sk-|sk_)[A-Za-z0-9_\-]{16,}"),  # OpenAI-style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # private key blocks
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}"),  # bearer tokens
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"xox[bpoa]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),  # email address
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),  # mainland China mobile
    re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),  # US SSN
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),  # mainland China citizen id
)

#: A long base64/hex run with no spaces is almost certainly an encoded secret,
#: not a fact worth remembering.
_OPAQUE_BLOB = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")

_REDACTED = "<redacted>"


def redact_secrets(text: str) -> str:
    """Return ``text`` with secret/PII patterns replaced by ``<redacted>``.

    Conservative: anything matching a secret pattern or a long opaque blob is
    replaced wholesale. The result is what memory extraction may store.
    """

    redacted = text
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    redacted = _OPAQUE_BLOB.sub(_REDACTED, redacted)
    return redacted


def contains_secret(text: str) -> bool:
    """Whether ``text`` carries a detectable secret (for eligibility checks)."""

    return any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS) or bool(_OPAQUE_BLOB.search(text))


__all__ = ["contains_secret", "redact_secrets"]
