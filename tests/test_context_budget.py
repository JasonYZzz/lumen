"""Tests for provider-aware token metering and model spec resolution (M2)."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart

from lumen.context.budget import (
    ConservativeTokenCounter,
    DeterministicTokenCounter,
    ProviderTokenCounter,
    TokenCount,
    raise_if_fixed_context_exceeds_window,
    resolve_model_spec,
    select_token_counter,
)
from lumen.context.legacy import ContextBudgetExceeded, estimate_message_tokens

# --------------------------------------------------------------------------- #
# CJK-aware estimation (acceptance: 中文估算不再使用统一 byte/4)
# --------------------------------------------------------------------------- #


def test_conservative_counter_counts_cjk_higher_than_byte_over_four() -> None:
    """CJK characters are metered at ~1 token each, not byte/4 (~0.75).

    byte/4 under-metered Chinese-heavy prompts (12 bytes / 4 = 3 for four
    common CJK chars), risking over-fill; the conservative counter meters 4.
    This is the M2 acceptance criterion that Chinese is no longer byte/4.
    """

    counter = ConservativeTokenCounter()
    text = "你好世界"  # 4 CJK chars, 12 UTF-8 bytes
    assert counter.count_text(text).tokens == 4
    # The legacy byte/4 estimator this replaces gave 3 for the same text.
    assert estimate_message_tokens([ModelRequest(parts=[UserPromptPart(content=text)])]) == 3


def test_conservative_counter_meters_ascii_at_byte_over_four() -> None:
    """ASCII text keeps the byte/4 coefficient (matches ASCII tokenizers)."""

    counter = ConservativeTokenCounter()
    assert counter.count_text("a" * 40).tokens == 10  # 40 bytes / 4


def test_conservative_counter_mixed_cjk_and_ascii() -> None:
    counter = ConservativeTokenCounter()
    # 2 CJK (2 tokens) + 8 ASCII bytes (2 tokens) = 4.
    count = counter.count_text("你好" + "a" * 8)
    assert count.tokens == 4
    assert count.bytes == len(("你好" + "a" * 8).encode("utf-8"))


def test_conservative_counter_counts_tool_schemas_with_framing() -> None:
    """Tool schemas add per-tool framing on top of the JSON text."""

    counter = ConservativeTokenCounter()
    one_tool = [{"name": "x", "description": "d", "parameters": {"type": "object"}}]
    two_tools = [*one_tool, {"name": "y", "description": "e", "parameters": {"type": "object"}}]
    one = counter.count_tools(one_tool)
    two = counter.count_tools(two_tools)
    # The second tool adds its JSON tokens plus the per-tool framing (5).
    assert two.tokens > one.tokens
    assert two.tokens - one.tokens >= 5  # at least the framing delta


def test_conservative_counter_messages_render_tool_parts() -> None:
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    counter = ConservativeTokenCounter()
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="read 你好")]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "x"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="内容", tool_call_id="c1")]),
    ]
    count = counter.count_messages(messages)
    assert count.tokens > 0
    # CJK in the user prompt and the tool return is metered at ~1/char.
    assert count.tokens >= 4  # 你好 (2) + 内容 (2) at minimum


def test_token_count_adds() -> None:
    assert TokenCount(3, 10) + TokenCount(2, 5) == TokenCount(5, 15)


# --------------------------------------------------------------------------- #
# Deterministic + provider adapters
# --------------------------------------------------------------------------- #


def test_deterministic_counter_returns_fixed_values() -> None:
    counter = DeterministicTokenCounter(per_message=10, per_tool=8)
    msgs = [ModelRequest(parts=[UserPromptPart(content="anything")])]
    assert counter.count_messages(msgs).tokens == 10
    assert counter.count_tools([{"name": "x"}, {"name": "y"}]).tokens == 16


def test_provider_counter_falls_back_to_conservative_without_tokenizer() -> None:
    """No tokenizer dependency wired -> provider counter behaves conservative."""

    provider = ProviderTokenCounter(count_text_tokens=None)
    conservative = ConservativeTokenCounter()
    assert provider.count_text("你好世界").tokens == conservative.count_text("你好世界").tokens


def test_provider_counter_uses_injected_tokenizer() -> None:
    def count_characters(text: str) -> int:
        return len(text)

    provider = ProviderTokenCounter(count_text_tokens=count_characters)  # 1 token/char
    assert provider.count_text("hello").tokens == 5


# --------------------------------------------------------------------------- #
# Model spec resolution (plan §8.1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model_id", "expected_window", "estimated"),
    [
        ("openai:gpt-4o", 128_000, False),
        ("openai:deepseek-v4-flash", 1_000_000, False),
        ("openai:glm-5.2", 1_000_000, False),
    ],
)
def test_known_model_resolves_to_profile(model_id: str, expected_window: int, estimated: bool) -> None:
    spec, is_estimated = resolve_model_spec(model_id)
    assert spec.context_window_tokens == expected_window
    assert is_estimated is estimated

def test_explicit_config_takes_precedence() -> None:
    spec, estimated = resolve_model_spec(
        "anthropic:claude-opus-4", explicit_window=50_000, explicit_max_output=1_000
    )
    assert spec.context_window_tokens == 50_000
    assert spec.max_output_tokens == 1_000
    assert estimated is False


def test_select_token_counter_returns_conservative_for_unknown() -> None:
    spec, _ = resolve_model_spec("acme:custom-7b")
    assert isinstance(select_token_counter(spec), ConservativeTokenCounter)


# --------------------------------------------------------------------------- #
# Fixed-context preflight (plan §8.2 step 3: 固定内容超限前置失败)
# --------------------------------------------------------------------------- #


def test_preflight_raises_when_fixed_plus_reserve_exceeds_window() -> None:
    with pytest.raises(ContextBudgetExceeded, match="fixed context footprint exceeds"):
        raise_if_fixed_context_exceeds_window(fixed_tokens=9_000, window_tokens=10_000, output_reserve=1_500)


def test_preflight_passes_when_fixed_plus_reserve_fits() -> None:
    # No raise.
    raise_if_fixed_context_exceeds_window(fixed_tokens=8_000, window_tokens=10_000, output_reserve=1_000)
