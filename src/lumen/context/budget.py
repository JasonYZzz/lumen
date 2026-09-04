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

:class:`resolve_model_spec` is a compatibility projection over the canonical
model-profile resolver. Resolution is explicit config > explicit profile >
exact model-slug alias > conservative fallback; the transport prefix is never
used as a model-family signal.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic_ai.messages import ModelMessage

from lumen.config import ContextConfig, ModelContextOverride, ModelSettingsConfig
from lumen.context.legacy import ContextBudgetExceeded, render_part_text
from lumen.context.profiles import TokenizerSpec, resolve_context_policy
from lumen.context.tokenizers import build_tiktoken_callable
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


@dataclass(frozen=True, slots=True)
class TokenCounterSelection:
    counter: TokenCounter
    adapter: str
    fallback_reason: str | None = None


class TokenCounterFactory:
    """Create offline counters from model-family tokenizer specifications."""

    @staticmethod
    def create(spec: TokenizerSpec) -> TokenCounterSelection:
        if spec.kind == "tiktoken":
            assert spec.encoding is not None
            callable_ = build_tiktoken_callable(spec.encoding)
            if callable_ is not None:
                return TokenCounterSelection(
                    ProviderTokenCounter(callable_),
                    adapter=f"tiktoken:{spec.encoding}",
                )
            return TokenCounterSelection(
                ConservativeTokenCounter(),
                adapter="conservative-cjk",
                fallback_reason=f"optional tiktoken encoding {spec.encoding!r} is unavailable",
            )
        if spec.kind == "deterministic":
            return TokenCounterSelection(DeterministicTokenCounter(), adapter="deterministic")
        return TokenCounterSelection(ConservativeTokenCounter(), adapter="conservative-cjk")


# --------------------------------------------------------------------------- #
# Model context spec resolution (plan §8.1)
# --------------------------------------------------------------------------- #


# Compatibility export retained for callers that imported the old table.
# Resolution no longer consults provider prefixes; all built-ins live in
# ``context.profiles`` and match exact model slugs.
_KNOWN_MODEL_WINDOWS: dict[str, tuple[int, int]] = {}


def resolve_model_spec(
    model_id: str,
    *,
    explicit_window: int | None = None,
    explicit_max_output: int | None = None,
) -> tuple[ModelContextSpec, bool]:
    """Resolve a model id to a :class:`ModelContextSpec` (plan §8.1).

    Deprecated compatibility projection over :func:`resolve_context_policy`.
    Provider prefixes are deliberately ignored; resolution is explicit fields
    > exact model profile alias > conservative fallback.
    """

    override = ModelContextOverride(
        window_tokens=explicit_window,
        max_output_tokens=explicit_max_output,
    )
    policy = resolve_context_policy(
        ModelSettingsConfig(id=model_id, context=override),
        ContextConfig(),
    )
    tokenizer = (
        f"tiktoken:{policy.tokenizer.encoding}"
        if policy.tokenizer.kind == "tiktoken"
        else policy.tokenizer.kind
    )
    return (
        ModelContextSpec(
            context_window_tokens=policy.context_window_tokens,
            max_output_tokens=policy.output_reserve_tokens,
            tokenizer=tokenizer,
        ),
        "context_window_tokens" in policy.estimated_fields,
    )


def select_token_counter(spec: ModelContextSpec, *, model_id: str | None = None) -> TokenCounter:
    """Pick the token counter for a resolved spec (plan §8.4).

    The tokenizer declared by the resolved model profile is used directly.
    Missing optional tokenizer dependencies fall back to the conservative
    counter without downloading files or making a network request.
    """

    tokenizer = spec.tokenizer
    if tokenizer.startswith("tiktoken:"):
        callable_ = build_tiktoken_callable(tokenizer.partition(":")[2])
        if callable_ is not None:
            return ProviderTokenCounter(count_text_tokens=callable_)
        return ProviderTokenCounter(count_text_tokens=None)
    if tokenizer.startswith("provider:"):
        # No exact offline tokenizer for this provider; stay conservative.
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
    "TokenCounterFactory",
    "TokenCounterSelection",
    "raise_if_fixed_context_exceeds_window",
    "resolve_model_spec",
    "select_token_counter",
]
