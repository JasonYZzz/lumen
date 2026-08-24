"""Tests for model-profile tokenizer wiring independent of transport."""

from __future__ import annotations

import sys

import pytest

from lumen.context import tokenizers
from lumen.context.budget import (
    ConservativeTokenCounter,
    ProviderTokenCounter,
    resolve_model_spec,
    select_token_counter,
)

tiktoken = pytest.importorskip("tiktoken")


def test_openai_counter_matches_tiktoken_exactly() -> None:
    spec, _ = resolve_model_spec("openai:gpt-4o")
    counter = select_token_counter(spec, model_id="openai:gpt-4o")
    assert isinstance(counter, ProviderTokenCounter)
    text = "Hello, 你好世界! this is a mixed prompt with emoji 🎉 and code `x = 1`."
    expected = len(tiktoken.get_encoding("o200k_base").encode(text))
    assert counter.count_text(text).tokens == expected


def test_openai_legacy_models_use_cl100k() -> None:
    spec, _ = resolve_model_spec("openai:gpt-4")
    counter = select_token_counter(spec, model_id="openai:gpt-4")
    text = "def main(): return 42"
    expected = len(tiktoken.get_encoding("cl100k_base").encode(text))
    assert counter.count_text(text).tokens == expected


def test_missing_tiktoken_falls_back_to_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate an environment without the optional dependency.
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    tokenizers._encoding.cache_clear()  # type: ignore[reportPrivateUsage]
    try:
        spec, _ = resolve_model_spec("openai:gpt-4o")
        counter = select_token_counter(spec, model_id="openai:gpt-4o")
        text = "你好世界"
        assert counter.count_text(text).tokens == ConservativeTokenCounter().count_text(text).tokens
    finally:
        tokenizers._encoding.cache_clear()  # type: ignore[reportPrivateUsage]


def test_non_openai_providers_stay_conservative() -> None:
    for model_id in ("anthropic:claude-opus-4", "google:gemini-2.5-pro", "moonshot:kimi-k2"):
        spec, _ = resolve_model_spec(model_id)
        counter = select_token_counter(spec, model_id=model_id)
        text = "你好世界 hello"
        assert counter.count_text(text).tokens == ConservativeTokenCounter().count_text(text).tokens


def test_engine_uses_counter_for_compaction_trigger() -> None:
    """The engine holds one counter shared by the assembler and the soft
    trigger, so CJK text no longer under-meters at byte/4 on the trigger path.
    """
    from lumen.config import ContextConfig
    from lumen.context.engine import ContextEngine

    engine = ContextEngine(config=ContextConfig(), model="test", model_id="openai:gpt-4o")
    cjk = "你好世界" * 100
    # Legacy byte/4 would say 300; the engine's wired counter meters exactly
    # like tiktoken (200 here), proving the trigger path uses the real one.
    exact = len(tiktoken.get_encoding("o200k_base").encode(cjk))
    assert engine._counter.count_text(cjk).tokens == exact != 300  # type: ignore[reportPrivateUsage]
