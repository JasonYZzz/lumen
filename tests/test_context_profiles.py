from __future__ import annotations

import pytest

from lumen.config import ContextConfig, ModelContextOverride, ModelSettingsConfig
from lumen.context import TokenCounterFactory, resolve_context_policy
from lumen.context.legacy import ContextStateChange, ContextSummary, merge_context_summary


@pytest.mark.parametrize(
    ("model_id", "profile", "window", "counter"),
    [
        ("openai:gpt-5.6", "openai-gpt-5.6", 1_050_000, "tiktoken"),
        ("openai:deepseek-flash", "deepseek-flash", 1_000_000, "conservative"),
        ("openai:deepseek-v4-flash", "conservative-fallback", 80_000, "conservative"),
        ("openai:kimi-k3", "kimi-k3", 1_000_000, "conservative"),
        ("openai:glm-5.2", "glm-5.2", 1_000_000, "conservative"),
        ("anthropic:qwen3.8-max", "alibaba-qwen3.8", 1_000_000, "conservative"),
        ("openai:qwen3.8-flash", "alibaba-qwen3.8", 1_000_000, "conservative"),
        ("openai:unknown-vendor", "conservative-fallback", 80_000, "conservative"),
    ],
)
def test_openai_compatible_transport_does_not_select_model_family(
    model_id: str,
    profile: str,
    window: int,
    counter: str,
) -> None:
    policy = resolve_context_policy(ModelSettingsConfig(id=model_id), ContextConfig())

    assert policy.profile_id == profile
    assert policy.context_window_tokens == window
    assert policy.tokenizer.kind == counter


def test_context_resolution_precedence_is_field_profile_alias_fallback() -> None:
    explicit = resolve_context_policy(
        ModelSettingsConfig(
            id="openai:unknown",
            context=ModelContextOverride(
                profile="kimi-k3",
                window_tokens=123_456,
                max_output_tokens=7_000,
            ),
        ),
        ContextConfig(),
    )
    profile = resolve_context_policy(
        ModelSettingsConfig(
            id="openai:unknown",
            context=ModelContextOverride(profile="glm-5.2"),
        ),
        ContextConfig(),
    )
    alias = resolve_context_policy(ModelSettingsConfig(id="openai:kimi-k3"), ContextConfig())
    fallback = resolve_context_policy(ModelSettingsConfig(id="openai:no-such-model"), ContextConfig())

    assert explicit.context_window_tokens == 123_456
    assert explicit.architectural_max_output_tokens == 7_000
    assert profile.profile_id == "glm-5.2"
    assert alias.profile_id == "kimi-k3"
    assert fallback.profile_id == "conservative-fallback"
    assert fallback.estimated
    assert fallback.output_reserve_tokens == 16_384

    large_unknown = resolve_context_policy(
        ModelSettingsConfig(
            id="openai:future-model",
            context=ModelContextOverride(window_tokens=1_000_000),
        ),
        ContextConfig(),
    )
    small_unknown = resolve_context_policy(
        ModelSettingsConfig(
            id="openai:small-local-model",
            context=ModelContextOverride(window_tokens=32_000),
        ),
        ContextConfig(),
    )
    assert large_unknown.output_reserve_tokens == 32_768
    assert small_unknown.output_reserve_tokens == 8_000


def test_requested_output_is_reserved_in_full_and_cannot_exceed_architecture() -> None:
    policy = resolve_context_policy(
        ModelSettingsConfig(id="openai:gpt-5.6", settings={"max_tokens": 65_536}),
        ContextConfig(),
    )
    assert policy.output_reserve_tokens == 65_536

    with pytest.raises(ValueError, match="exceeds"):
        resolve_context_policy(
            ModelSettingsConfig(id="openai:gpt-5.6", settings={"max_tokens": 128_001}),
            ContextConfig(),
        )

    qwen = resolve_context_policy(
        ModelSettingsConfig(id="anthropic:qwen3.8-max"),
        ContextConfig(),
    )
    assert qwen.architectural_max_output_tokens == 131_072
    assert qwen.output_reserve_tokens == 131_072


def test_tokenizer_factory_never_downloads_and_has_local_fallback() -> None:
    policy = resolve_context_policy(ModelSettingsConfig(id="openai:gpt-5.6"), ContextConfig())
    selected = TokenCounterFactory.create(policy.tokenizer)

    assert selected.adapter in {"tiktoken:o200k_base", "conservative-cjk"}
    if selected.adapter == "conservative-cjk":
        assert selected.fallback_reason


def test_rolling_summary_cannot_silently_delete_protected_state() -> None:
    previous = ContextSummary(
        goals=["ship"],
        constraints=["do not delete /tmp/evidence.log"],
        completed=[],
        current_plan=["verify"],
        important_files=["src/lumen/context/engine.py"],
        key_facts=["ERR_SESSION_42"],
        failures_and_approvals=["approved production read"],
        outstanding=["run integration test"],
    )

    merged = merge_context_summary(previous, ContextSummary(completed=["run integration test"]))

    assert merged.constraints == previous.constraints
    assert merged.important_files == previous.important_files
    assert merged.key_facts == previous.key_facts
    assert merged.failures_and_approvals == previous.failures_and_approvals
    assert "run integration test" not in merged.outstanding


def test_rolling_summary_applies_only_explicit_revocation_and_supersession() -> None:
    previous = ContextSummary(
        constraints=["deploy only after approval"],
        important_files=["src/old.py"],
        key_facts=["error_code: ERR_OLD"],
    )
    candidate = ContextSummary(
        state_changes=[
            ContextStateChange(
                field="constraints",
                action="revoke",
                target="deploy only after approval",
                reason="the user explicitly withdrew the constraint",
            ),
            ContextStateChange(
                field="important_files",
                action="supersede",
                target="src/old.py",
                replacement="src/new.py",
                reason="the file was renamed",
            ),
            ContextStateChange(
                field="exact_literals",
                action="supersede",
                target="ERR_OLD",
                replacement="error_code: ERR_NEW",
                reason="the new run returned a replacement code",
            ),
        ]
    )

    merged = merge_context_summary(previous, candidate)

    assert merged.constraints == []
    assert merged.important_files == ["src/new.py"]
    assert merged.key_facts == ["error_code: ERR_NEW"]
    assert merged.state_changes == candidate.state_changes
