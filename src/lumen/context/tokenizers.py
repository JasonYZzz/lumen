"""Optional real-tokenizer adapters (P0: accurate compaction triggers).

The budget module's :class:`~lumen.context.budget.ProviderTokenCounter` already
accepts a ``text -> tokens`` callable; this module builds that callable from
``tiktoken`` when it is installed. The dependency is deliberately optional:
when ``tiktoken`` is missing the builder returns ``None`` and budgeting falls
back to the conservative estimator, so production never hard-fails on an
import.

Only the ``openai`` provider prefix is wired to an exact encoding. Anthropic
and Google tokenizers are not publicly available as offline libraries, and
guessing a wrong encoding is worse than the conservative over-estimate, so
those prefixes keep the fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

#: Models whose tokenizer is ``o200k_base``; everything else OpenAI uses
#: ``cl100k_base``. Matched as substrings of the model id.
_O200K_MODEL_MARKERS = ("gpt-4o", "gpt-4.1", "gpt-5", "o1", "o3", "o4")


@lru_cache(maxsize=4)
def _encoding(name: str) -> Callable[[str], int]:
    """Return a cached ``text -> token count`` for a tiktoken encoding.

    Importing inside the function keeps the module importable (and the
    fallback path testable) when ``tiktoken`` is not installed.
    """

    import tiktoken

    encoder = tiktoken.get_encoding(name)
    return lambda text: len(encoder.encode(text))


def build_openai_token_callable(model_id: str) -> Callable[[str], int] | None:
    """Build an exact token counter for an OpenAI model id, or ``None``.

    ``None`` means "no exact tokenizer available" (missing dependency) and the
    caller must fall back to the conservative counter.
    """

    try:
        encoding_name = (
            "o200k_base"
            if any(marker in model_id for marker in _O200K_MODEL_MARKERS)
            else "cl100k_base"
        )
        return _encoding(encoding_name)
    except ImportError:
        return None


def build_tiktoken_callable(encoding_name: str) -> Callable[[str], int] | None:
    """Return a local tiktoken counter for an explicit encoding.

    Capability profiles select encodings by model family; the provider
    transport is deliberately irrelevant. Missing optional dependencies remain
    a soft fallback.
    """

    try:
        return _encoding(encoding_name)
    except ImportError:
        return None


__all__ = ["build_openai_token_callable", "build_tiktoken_callable"]
