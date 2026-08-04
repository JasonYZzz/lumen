"""Model capability profiles, independent from provider transport adapters.

The ``openai:`` prefix selects a wire protocol in :mod:`lumen.models`; it must
not imply an OpenAI tokenizer or context window.  This module resolves model
slugs to explicit, source-backed capabilities and produces the one policy used
by context budgeting, compaction, and provider preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from lumen.config import ContextConfig, ModelSettingsConfig, ModelTokenizerConfig

TokenizerKind = Literal["conservative", "tiktoken", "deterministic"]


@dataclass(frozen=True, slots=True)
class TokenizerSpec:
    kind: TokenizerKind
    encoding: str | None = None


@dataclass(frozen=True, slots=True)
class ModelCapabilityProfile:
    id: str
    aliases: tuple[str, ...]
    context_window_tokens: int
    max_output_tokens: int | None
    tokenizer: TokenizerSpec
    source: str
    verified_at: date


@dataclass(frozen=True, slots=True)
class ResolvedContextPolicy:
    profile_id: str
    profile_source: str
    context_window_tokens: int
    architectural_max_output_tokens: int | None
    output_reserve_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    target_tokens: int
    keep_recent_tokens: int
    tokenizer: TokenizerSpec
    estimated_fields: frozenset[str]
    legacy_soft_override: bool = False
    legacy_recent_override: bool = False

    @property
    def estimated(self) -> bool:
        return bool(self.estimated_fields)


_PROFILES: tuple[ModelCapabilityProfile, ...] = (
    ModelCapabilityProfile(
        id="openai-gpt-5.6",
        aliases=("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
        context_window_tokens=1_050_000,
        max_output_tokens=128_000,
        tokenizer=TokenizerSpec("tiktoken", "o200k_base"),
        source="https://developers.openai.com/api/docs/models",
        verified_at=date(2026, 7, 31),
    ),
    ModelCapabilityProfile(
        id="openai-gpt-4o",
        aliases=("gpt-4o",),
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        tokenizer=TokenizerSpec("tiktoken", "o200k_base"),
        source="builtin:legacy-known-model-table",
        verified_at=date(2026, 7, 31),
    ),
    ModelCapabilityProfile(
        id="openai-gpt-4",
        aliases=("gpt-4",),
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        tokenizer=TokenizerSpec("tiktoken", "cl100k_base"),
        source="builtin:legacy-known-model-table",
        verified_at=date(2026, 7, 31),
    ),
    ModelCapabilityProfile(
            id="deepseek-v4-flash",
            aliases=("deepseek-v4-flash",),
            context_window_tokens=1_000_000,
            max_output_tokens=384_000,
            tokenizer=TokenizerSpec("conservative"),
            source="https://api-docs.deepseek.com/quick_start/pricing/",
            verified_at=date(2026, 7, 31),
        ),
    ModelCapabilityProfile(
        id="deepseek-v4-pro",
        aliases=("deepseek-v4-pro",),
        context_window_tokens=1_000_000,
        max_output_tokens=384_000,
        tokenizer=TokenizerSpec("conservative"),
        source="https://api-docs.deepseek.com/quick_start/pricing/",
        verified_at=date(2026, 7, 31),
    ),
    ModelCapabilityProfile(
        id="kimi-k3",
        aliases=("kimi-k3",),
        context_window_tokens=1_000_000,
        max_output_tokens=None,
        tokenizer=TokenizerSpec("conservative"),
        source="https://www.kimi.com/zh-cn/help/kimi-api/api-troubleshooting",
        verified_at=date(2026, 7, 31),
    ),
    ModelCapabilityProfile(
        id="glm-5.2",
        aliases=("glm-5.2",),
        context_window_tokens=1_000_000,
        max_output_tokens=None,
        tokenizer=TokenizerSpec("conservative"),
        source="https://z.ai/blog/glm-5.2",
        verified_at=date(2026, 7, 31),
    ),
    # Existing native-provider defaults stay available by model-family alias,
    # never by transport prefix. These conservative profiles preserve current
    # behaviour for supported Claude/Gemini deployments without misclassifying
    # OpenAI-compatible third-party endpoints.

)

_BY_ID = {profile.id: profile for profile in _PROFILES}
_BY_ALIAS = {alias.lower(): profile for profile in _PROFILES for alias in profile.aliases}


def profiles() -> tuple[ModelCapabilityProfile, ...]:
    return _PROFILES


def _slug(model_id: str) -> str:
    return model_id.split(":", 1)[-1].strip().lower()


def _tokenizer_override(value: ModelTokenizerConfig | None, inherited: TokenizerSpec) -> TokenizerSpec:
    if value is None or value.kind == "auto":
        return inherited
    if value.kind == "tiktoken":
        return TokenizerSpec("tiktoken", value.encoding)
    return TokenizerSpec("conservative")


def resolve_context_policy(
    model: ModelSettingsConfig,
    context: ContextConfig,
) -> ResolvedContextPolicy:
    """Resolve one model to a complete, validated context policy.

    Precedence is explicit model fields, explicit profile, exact slug alias,
    then a conservative fallback. Provider/wire prefixes are intentionally not
    consulted.
    """

    override = model.context
    if override.profile is not None:
        try:
            profile = _BY_ID[override.profile]
        except KeyError as error:
            raise ValueError(f"unknown model context profile {override.profile!r}") from error
    else:
        profile = _BY_ALIAS.get(_slug(model.id))

    estimated: set[str] = set()
    if profile is None:
        profile_id = "conservative-fallback"
        source = "fallback:unknown-model"
        window = 80_000
        architecture_output: int | None = None
        tokenizer = TokenizerSpec("conservative")
        estimated.update({"profile", "context_window_tokens", "max_output_tokens", "tokenizer"})
    else:
        profile_id = profile.id
        source = profile.source
        window = profile.context_window_tokens
        architecture_output = profile.max_output_tokens
        tokenizer = profile.tokenizer
        if architecture_output is None:
            estimated.add("max_output_tokens")

    if override.window_tokens is not None:
        window = override.window_tokens
        estimated.discard("context_window_tokens")
    if override.max_output_tokens is not None:
        architecture_output = override.max_output_tokens
        estimated.discard("max_output_tokens")
    tokenizer = _tokenizer_override(override.tokenizer, tokenizer)
    if override.tokenizer is not None and override.tokenizer.kind != "auto":
        estimated.discard("tokenizer")

    configured_output_raw = model.settings.get("max_tokens")
    configured_output = (
        int(configured_output_raw)
        if isinstance(configured_output_raw, int) and not isinstance(configured_output_raw, bool)
        else None
    )
    if configured_output is not None and configured_output <= 0:
        raise ValueError("model settings.max_tokens must be positive")
    if (
        configured_output is not None
        and architecture_output is not None
        and configured_output > architecture_output
    ):
        raise ValueError(
            "model settings.max_tokens exceeds the resolved model capability: "
            f"{configured_output} > {architecture_output}"
        )
    if configured_output is not None:
        output_reserve = configured_output
    elif architecture_output is not None:
        output_reserve = architecture_output
    else:
        output_reserve = 4_096
        estimated.add("output_reserve_tokens")

    legacy_soft = "soft_token_limit" in context.model_fields_set
    legacy_recent = "keep_recent_tokens" in context.model_fields_set
    soft = (
        override.soft_limit_tokens
        if override.soft_limit_tokens is not None
        else context.soft_token_limit
        if legacy_soft
        else int(window * context.soft_ratio)
    )
    hard = int(window * context.hard_ratio)
    ratio_target = int(window * context.target_ratio)
    # An absolute soft limit is a compatibility/operations override, not a
    # replacement model window. It may intentionally sit above the resolved
    # hard limit to disable proactive compaction in tests or deployments. Keep
    # the provider hard limit authoritative and derive a usable target below a
    # small explicit soft limit.
    has_absolute_soft = override.soft_limit_tokens is not None or legacy_soft
    target = (
        max(1, min(ratio_target, soft - 1))
        if has_absolute_soft and soft > 1
        else ratio_target
    )
    recent = override.keep_recent_tokens or context.keep_recent_tokens
    if has_absolute_soft:
        valid_thresholds = 0 < target and 0 < soft and 0 < hard <= window
    else:
        valid_thresholds = 0 < target < soft < hard <= window
    if not valid_thresholds:
        raise ValueError(
            "invalid resolved context thresholds "
            f"(target={target}, soft={soft}, hard={hard}, window={window})"
        )
    if output_reserve >= window:
        raise ValueError(
            f"resolved output reserve {output_reserve} must be smaller than context window {window}"
        )

    return ResolvedContextPolicy(
        profile_id=profile_id,
        profile_source=source,
        context_window_tokens=window,
        architectural_max_output_tokens=architecture_output,
        output_reserve_tokens=output_reserve,
        soft_limit_tokens=soft,
        hard_limit_tokens=hard,
        target_tokens=target,
        keep_recent_tokens=recent,
        tokenizer=tokenizer,
        estimated_fields=frozenset(estimated),
        legacy_soft_override=legacy_soft,
        legacy_recent_override=legacy_recent,
    )


__all__ = [
    "ModelCapabilityProfile",
    "ResolvedContextPolicy",
    "TokenizerSpec",
    "profiles",
    "resolve_context_policy",
]
