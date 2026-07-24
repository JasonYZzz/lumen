"""Provider-aware token metering and model context resolution (M2).

Replaces the legacy uniform ``len(utf8_bytes) / 4`` estimator with adapters
that reflect provider tokenizers, so CJK text and tool-schema JSON no longer
share one crude coefficient (plan §3.2 #6, §8.1, §8.4).

Three adapters (plan §8.4):

* :class:`ConservativeTokenCounter` - the default for unknown providers. Counts
  CJK characters at ~1 token each (cl100k encodes common CJK as single tokens;
  byte/4 gave 0.75 and silently under-metered Chinese-heavy prompts) and other
  text at byte/4. JSON/tool schemas add per-tool framing.
* :class:`DeterministicTokenCounter` - a fixed-table adapter for tests.
* :class:`ProviderTokenCounter` - wraps a provider tokenizer when one is
  available; falls back to the conservative counter when the optional
  dependency is missing, so production never hard-fails on a tokenizer import.

:class:`resolve_model_spec` resolves a model id to a :class:`ModelContextSpec`
via explicit config > provider profile > known-model table > conservative
default (plan §8.1), marking the fallback so ``/context`` can flag ``estimated``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from lumen.context.legacy import ContextBudgetExceeded, render_part_text
from lumen.context.types import ModelContextSpec

# --------------------------------------------------------------------------- #
# Token counts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A metered token total plus the raw byte size used to derive it.

    ``bytes`` is carried so the engine can report estimated-vs-actual drift:
    providers bill by their tokenizer, and ``bytes`` lets a calibration step
    compare against the legacy byte/4 baseline without re-serialising.
    """

    tokens: int
    bytes: int = 0

    def __add__(self, other: TokenCount) -> TokenCount:
        return TokenCount(self.tokens + other.tokens, self.bytes + other.bytes)


# --------------------------------------------------------------------------- #
# Token counter protocol and adapters
# --------------------------------------------------------------------------- #


class TokenCounter(Protocol):
    """Meter messages, tool schemas and free text to a token total (plan §8.4)."""

    def count_messages(self, messages: Sequence[ModelMessage]) -> TokenCount: ...

    def count_tools(self, schemas: Sequence[Any]) -> TokenCount: ...

    def count_text(self, text: str) -> TokenCount: ...


#: Per-tool framing overhead the conservative counter adds on top of the
#: serialised schema: providers wrap each tool definition in role/structure
#: tokens that byte/4 of the JSON alone under-counts (plan §8.4 "tool schema
#: 使用不同系数").
_TOOL_FRAMING_TOKENS = 5


def _is_cjk(codepoint: int) -> bool:
    """Whether ``codepoint`` is a CJK ideograph or kana/hangul/fullwidth glyph.

    These are encoded by real tokenizers at ~1 token per character rather than
    the byte/4 (~0.75) the legacy estimator assumed, which under-metered
    Chinese-heavy prompts and risked over-fill.
    """

    return (
        0x3000 <= codepoint <= 0x9FFF  # CJK symbols, kana, unified ideographs
        or 0xAC00 <= codepoint <= 0xD7AF  # Hangul syllables
        or 0xF900 <= codepoint <= 0xFAFF  # CJK compatibility ideographs
        or 0xFF00 <= codepoint <= 0xFFEF  # fullwidth forms
        or 0x3400 <= codepoint <= 0x4DBF  # CJK extension A
        or 0x20000 <= codepoint <= 0x2FFFF  # CJK extension B-F
    )


class ConservativeTokenCounter:
    """CJK- and JSON-aware estimator for providers without a known tokenizer.

    Conservative in the budgeting sense: it over-estimates rather than
    under-estimates, so a request is rejected before it over-fills the window.
    """

    def count_text(self, text: str) -> TokenCount:
        if not text:
            return TokenCount(0, 0)
        cjk = 0
        other_bytes = 0
        for char in text:
            if _is_cjk(ord(char)):
                cjk += 1
            else:
                other_bytes += len(char.encode("utf-8"))
        # CJK ~1 token/char; remaining text ~byte/4 (matches ASCII tokenizers).
        tokens = cjk + math.ceil(other_bytes / 4)
        return TokenCount(tokens, len(text.encode("utf-8")))

    def count_messages(self, messages: Sequence[ModelMessage]) -> TokenCount:
        total = TokenCount(0, 0)
        for message in messages:
            for part in getattr(message, "parts", []):
                total = total + self.count_text(render_part_text(part))
        return total

    def count_tools(self, schemas: Sequence[Any]) -> TokenCount:
        if not schemas:
            return TokenCount(0, 0)
        rendered = json.dumps(list(schemas), ensure_ascii=False, sort_keys=True, default=str)
        text_count = self.count_text(rendered)
        framing = _TOOL_FRAMING_TOKENS * len(list(schemas))
        return TokenCount(text_count.tokens + framing, text_count.bytes)


class DeterministicTokenCounter:
    """Fixed-table counter for tests; no byte inspection, fully predictable."""

    def __init__(self, *, per_message: int = 10, per_tool: int = 8, per_text_char: float = 0.0) -> None:
        self._per_message = per_message
        self._per_tool = per_tool
        self._per_text_char = per_text_char

    def count_text(self, text: str) -> TokenCount:
        return TokenCount(int(self._per_text_char * len(text)), len(text.encode("utf-8")))

    def count_messages(self, messages: Sequence[ModelMessage]) -> TokenCount:
        return TokenCount(self._per_message * len(list(messages)), 0)

    def count_tools(self, schemas: Sequence[Any]) -> TokenCount:
        return TokenCount(self._per_tool * len(list(schemas)), 0)


class ProviderTokenCounter:
    """Wraps a provider tokenizer when available, else falls back to conservative.

    The optional ``count`` callable receives the text and returns the
    provider's token count; when ``None`` (the typical case until a tokenizer
    dependency is wired) this adapter behaves as a
    :class:`ConservativeTokenCounter` so production never fails on a missing
    tokenizer.
    """

    def __init__(self, count_text_tokens: Any = None) -> None:
        self._fallback = ConservativeTokenCounter()
        self._count = count_text_tokens

    def count_text(self, text: str) -> TokenCount:
        if self._count is None:
            return self._fallback.count_text(text)
        tokens = int(self._count(text))
        return TokenCount(tokens, len(text.encode("utf-8")))

    def count_messages(self, messages: Sequence[ModelMessage]) -> TokenCount:
        if self._count is None:
            return self._fallback.count_messages(messages)
        total = TokenCount(0, 0)
        for message in messages:
            for part in getattr(message, "parts", []):
                total = total + self.count_text(render_part_text(part))
        return total

    def count_tools(self, schemas: Sequence[Any]) -> TokenCount:
        # Tool-schema tokenization differs across providers; without a provider
        # tokenizer that understands tool framing, the conservative counter's
        # framing overhead is the safer estimate.
        return self._fallback.count_tools(schemas)


# --------------------------------------------------------------------------- #
# Model context spec resolution (plan §8.1)
# --------------------------------------------------------------------------- #


#: Conservative default when no profile or known model matches. Marked
#: ``estimated`` so ``/context`` flags that the window is a fallback.
_DEFAULT_WINDOW_TOKENS = 32_000
_DEFAULT_MAX_OUTPUT_TOKENS = 4_096

#: Known model windows keyed by provider prefix (plan §8.1 "已知模型表").
#: Windows are upper bounds from public docs; the conservative default covers
#: anything not listed here. Kept small and public so calibration can extend it.
_KNOWN_MODEL_WINDOWS: dict[str, tuple[int, int]] = {
    # provider prefix -> (context_window_tokens, max_output_tokens)
    "anthropic": (200_000, 8_192),
    "openai": (128_000, 16_384),
    "google": (1_000_000, 8_192),
    "gemini": (1_000_000, 8_192),
    "deepseek": (128_000, 8_192),
    "openrouter": (128_000, 8_192),
    "zhipu": (128_000, 4_096),
    "glm": (128_000, 4_096),
    "moonshot": (128_000, 8_192),
    "mistral": (128_000, 8_192),
}


def resolve_model_spec(
    model_id: str,
    *,
    explicit_window: int | None = None,
    explicit_max_output: int | None = None,
) -> tuple[ModelContextSpec, bool]:
    """Resolve a model id to a :class:`ModelContextSpec` (plan §8.1).

    Resolution order: explicit config > known-model table (by provider prefix)
    > conservative default. Returns the spec plus an ``estimated`` flag (True
    when the window came from the conservative default rather than a known
    profile or explicit config) so ``/context`` can mark it.
    """

    if explicit_window is not None and explicit_window > 0:
        return (
            ModelContextSpec(
                context_window_tokens=explicit_window,
                max_output_tokens=explicit_max_output or _DEFAULT_MAX_OUTPUT_TOKENS,
                tokenizer="explicit",
            ),
            False,
        )
    prefix = model_id.split(":", 1)[0].lower() if model_id else ""
    known = _KNOWN_MODEL_WINDOWS.get(prefix)
    if known is not None:
        window, max_output = known
        return (
            ModelContextSpec(
                context_window_tokens=window,
                max_output_tokens=explicit_max_output or max_output,
                tokenizer=f"provider:{prefix}",
            ),
            False,
        )
    return (
        ModelContextSpec(
            context_window_tokens=_DEFAULT_WINDOW_TOKENS,
            max_output_tokens=explicit_max_output or _DEFAULT_MAX_OUTPUT_TOKENS,
            tokenizer="conservative",
        ),
        True,
    )


def select_token_counter(spec: ModelContextSpec) -> TokenCounter:
    """Pick the token counter for a resolved spec (plan §8.4).

    A real provider tokenizer is wired by constructing a
    :class:`ProviderTokenCounter` with the tokenizer callable; until then the
    conservative counter is the safe default.
    """

    tokenizer = spec.tokenizer
    if tokenizer.startswith("provider:"):
        # No tokenizer dependency is wired yet; fall back to conservative.
        return ProviderTokenCounter(count_text_tokens=None)
    return ConservativeTokenCounter()


def raise_if_fixed_context_exceeds_window(
    *, fixed_tokens: int, window_tokens: int, output_reserve: int
) -> None:
    """Reject a request whose fixed footprint cannot fit before calling provider.

    Implements the M2 safety invariant (plan §3.2 #5, §8.2 step 3): a request
    whose stable prefix + current input + output reserve already exceeds the
    model window must fail loudly before any provider call, rather than be sent
    to over-fill the window.
    """

    if fixed_tokens + output_reserve > window_tokens:
        raise ContextBudgetExceeded(
            "fixed context footprint exceeds the model window before history is "
            f"added: {fixed_tokens} fixed + {output_reserve} output reserve > "
            f"{window_tokens} window tokens"
        )


__all__ = [
    "_KNOWN_MODEL_WINDOWS",
    "ConservativeTokenCounter",
    "DeterministicTokenCounter",
    "ProviderTokenCounter",
    "TokenCount",
    "TokenCounter",
    "raise_if_fixed_context_exceeds_window",
    "resolve_model_spec",
    "select_token_counter",
]
