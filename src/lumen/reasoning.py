"""Resolve application reasoning choices once, before freezing a model request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from lumen.provider_catalog import (
    CATALOG_REVISION,
    RULES,
    ModelProtocol,
    ReasoningCodec,
    find_reasoning_rule,
    model_protocol,
    provider_vendor,
)
from lumen.provider_catalog import ReasoningLevel as ReasoningLevel

if TYPE_CHECKING:
    from lumen.config import ModelSettingsConfig


# Lumen's existing budget policy, not provider-native effort levels. Keeping
# explicit values prevents an SDK upgrade silently changing frozen preferences.
_BUDGETS = {ReasoningLevel.MINIMAL: 1024, ReasoningLevel.LOW: 2048,
            ReasoningLevel.MEDIUM: 10000, ReasoningLevel.HIGH: 16384}


class ThinkingBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["enabled", "disabled", "adaptive"]
    budget_tokens: int | None = Field(default=None, ge=1024)


class GoogleThinkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    thinking_level: Literal["MINIMAL", "LOW", "MEDIUM", "HIGH"]
    include_thoughts: bool = True


class ReasoningParameters(BaseModel):
    """Credential-free subset safe to freeze in the Session journal."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    thinking: bool | Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    openai_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    anthropic_thinking: ThinkingBudget | None = None
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Only this typed extension may enter a snapshot, never arbitrary extra_body.
    wire_thinking: Literal["enabled", "disabled"] | None = None
    google_thinking_config: GoogleThinkingConfig | None = None

    def settings(self) -> dict[str, Any]:
        result = self.model_dump(mode="json", exclude_none=True, exclude={"wire_thinking"})
        if self.wire_thinking is not None:
            result["extra_body"] = {"thinking": {"type": self.wire_thinking}}
        return result


REASONING_KEYS = frozenset(("thinking", "openai_reasoning_effort", "anthropic_thinking",
                            "anthropic_effort", "google_thinking_config"))
_EXTRA_KEYS = frozenset(("thinking", "reasoning", "reasoning_effort", "enable_thinking", "thinking_budget"))


class ReasoningSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requested: ReasoningLevel | None = None
    effective: ReasoningLevel | None = None
    source: str = "provider_default"
    mapping: str = "provider_default"
    supported_levels: tuple[ReasoningLevel, ...] = (ReasoningLevel.PROVIDER_DEFAULT,)
    parameters: ReasoningParameters = Field(default_factory=ReasoningParameters)
    capability_status: Literal["supported", "unsupported", "unknown"] = "unknown"
    capability_source: str = "unknown"
    catalog_revision: str | None = None
    provider: str | None = None
    capability_documents: tuple[str, ...] = ()
    capability_reviewed_on: str | None = None
    capability_note: str = ""
    # Official omission behavior, not a parameter sent or observed on this request.
    provider_default_level: ReasoningLevel | None = None
    level_map: dict[ReasoningLevel, ReasoningLevel] = Field(
        default_factory=dict[ReasoningLevel, ReasoningLevel],
    )


def _capability(config: ModelSettingsConfig) -> ReasoningSelection:
    rule = find_reasoning_rule(config.id, config.api, config.base_url)
    if config.reasoning_profile is not None:
        candidates = [item for item in RULES if item.key == config.reasoning_profile]
        if len(candidates) != 1:
            raise ValueError("unknown reasoning_profile; select a reviewed provider catalog rule")
        declared = candidates[0]
        vendor = provider_vendor(config.id, config.api, config.base_url)
        if vendor is not None and vendor != declared.vendor:
            raise ValueError("reasoning_profile conflicts with the known provider endpoint")
        if config.id.partition(":")[2] not in declared.models or model_protocol(
            config.id, config.api,
        ) not in declared.protocols:
            raise ValueError("reasoning_profile must match the exact model ID and API protocol")
        if rule is not None and rule != declared:
            raise ValueError("reasoning_profile conflicts with the known provider endpoint")
        rule = declared
    mapping = rule.level_map() if rule is not None else {}
    codec = rule.codec if rule is not None else ReasoningCodec.NONE
    origin = ("deployment_profile:" if config.reasoning_profile else "official_docs:") + rule.key if (
        rule is not None
    ) else "unknown"
    status: Literal["supported", "unsupported", "unknown"] = (
        "supported" if mapping else "unsupported" if rule is not None else "unknown"
    )
    if config.reasoning_levels is not None:
        if rule is not None:
            invalid = set(config.reasoning_levels) - mapping.keys()
            if invalid:
                raise ValueError(
                    "reasoning_levels includes unsupported model levels: " + ", ".join(sorted(invalid))
                )
            mapping = {value: mapping[value] for value in config.reasoning_levels}
        else:
            # Compatibility for existing deployment declarations. This is the user's
            # assertion, never a supplier-verified capability discovered from the name.
            codec = {
                ModelProtocol.CHAT: ReasoningCodec.OPENAI,
                ModelProtocol.RESPONSES: ReasoningCodec.OPENAI,
                ModelProtocol.ANTHROPIC: ReasoningCodec.ANTHROPIC_BUDGET,
                ModelProtocol.GOOGLE: ReasoningCodec.GOOGLE_LEVEL,
            }.get(model_protocol(config.id, config.api), ReasoningCodec.NONE)
            if codec is ReasoningCodec.NONE and config.reasoning_levels:
                raise ValueError("no reasoning adapter for this provider")
            mapping = {value: value for value in config.reasoning_levels}
            origin = "deployment_declaration"
        status = "supported" if mapping else "unsupported"
    return ReasoningSelection(
        mapping=codec.value, capability_status=status, capability_source=origin, level_map=mapping,
        supported_levels=(ReasoningLevel.PROVIDER_DEFAULT, *(
            value for value in ReasoningLevel if value in mapping
        )), catalog_revision=CATALOG_REVISION if rule is not None else None,
        provider=rule.vendor if rule is not None else None,
        capability_documents=rule.sources if rule is not None else (),
        capability_reviewed_on=rule.reviewed_on if rule is not None else None,
        capability_note=rule.note if rule is not None else "",
        provider_default_level=(rule.default_level
                                if rule is not None and not config.reasoning_profile else None),
    )


def resolve_reasoning(
    config: ModelSettingsConfig,
    effort: ReasoningLevel | None = None,
    *,
    source: str = "model",
) -> ReasoningSelection:
    """Validate explicit choices; omitted legacy settings retain their wire semantics.

    Unknown deployments must declare reasoning_levels before the application
    offers an effort selector. The existing raw settings escape hatch remains
    compatible, but is labelled unverified instead of claiming server behavior.
    """
    capability = _capability(config)
    levels = capability.supported_levels
    selected = effort if effort is not None else config.reasoning_effort
    if selected is None:
        # Copy only well-typed inference fields. Other settings, including
        # credentials/extra_body, never enter this audit snapshot.
        raw = {key: value for key, value in config.settings.items() if key in REASONING_KEYS}
        extra = _extra_body(config.settings)
        has_extra = (
            bool(_EXTRA_KEYS.intersection(extra)) or
            (isinstance(extra.get("output_config"), dict) and "effort" in extra["output_config"])
        )
        try:
            legacy_parameters = ReasoningParameters.model_validate(raw)
        except ValueError:
            return capability.model_copy(update={"source": "legacy_settings", "mapping": "unverified"})
        return capability.model_copy(update={"source": "legacy_settings" if raw or has_extra else
                                             "provider_default", "mapping": "unverified" if raw or has_extra
                                             else "provider_default", "parameters": legacy_parameters})
    if selected not in levels:
        raise ValueError(f"{config.id} does not declare thinking={selected.value}; available: "
                         + ", ".join(levels) + ". Configure reasoning_levels for a verified deployment.")
    if selected is ReasoningLevel.PROVIDER_DEFAULT:
        return capability.model_copy(update={"requested": selected, "source": source,
                                             "mapping": "provider_default"})
    parameters: dict[str, Any]
    effective = capability.level_map[selected]
    if capability.mapping in {"openai_effort", "deepseek_chat"}:
        parameters = {
            "openai_reasoning_effort": "none" if selected is ReasoningLevel.OFF else effective.value,
        }
        if capability.mapping == "deepseek_chat":
            parameters["wire_thinking"] = "disabled" if selected is ReasoningLevel.OFF else "enabled"
            if selected is ReasoningLevel.OFF:
                parameters.pop("openai_reasoning_effort")
    elif capability.mapping.startswith("anthropic_"):
        if selected is ReasoningLevel.OFF:
            parameters = {"anthropic_thinking": {"type": "disabled"}}
        elif capability.mapping == "anthropic_effort":
            parameters = {"anthropic_thinking": {"type": "enabled"}, "anthropic_effort": effective.value}
        else:
            if selected in {ReasoningLevel.XHIGH, ReasoningLevel.MAX}:
                raise ValueError("budget thinking does not support xhigh/max; choose high")
            budget = _BUDGETS[selected]
            cap = config.settings.get("max_tokens")
            if isinstance(cap, int) and budget + 1024 > cap:
                raise ValueError(f"thinking budget {budget} needs max_tokens >= {budget + 1024}; "
                                 "choose a lower effort or explicitly change the output cap")
            parameters = {"anthropic_thinking": {"type": "enabled", "budget_tokens": budget}}
    elif capability.mapping == "google_level":
        if selected not in {ReasoningLevel.MINIMAL, ReasoningLevel.LOW,
                            ReasoningLevel.MEDIUM, ReasoningLevel.HIGH}:
            raise ValueError("Google level-based thinking only supports minimal/low/medium/high")
        parameters = {"google_thinking_config": {"thinking_level": effective.value.upper(),
                                                 "include_thoughts": True}}
    else:
        raise ValueError("no application thinking mapping; use provider settings")
    return capability.model_copy(update={"requested": selected, "effective": effective, "source": source,
                                          "mapping": "native",
                                          "parameters": ReasoningParameters.model_validate(parameters)})


def _extra_body(settings: dict[str, Any]) -> dict[str, Any]:
    value: object = settings.get("extra_body")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("extra_body must be an object")
    return cast(dict[str, Any], value)


def apply_reasoning(settings: dict[str, Any], selection: ReasoningSelection) -> dict[str, Any]:
    """Apply one authoritative override without mutating the model's configuration."""
    if selection.requested is None:
        if selection.source == "legacy_settings" and selection.parameters.settings():
            return {**{key: value for key, value in settings.items() if key not in REASONING_KEYS},
                    **selection.parameters.settings()}
        return dict(settings)
    result = {key: value for key, value in settings.items() if key not in REASONING_KEYS}
    extra = _extra_body(result)
    if extra:
        cleaned = {key: value for key, value in extra.items() if key not in _EXTRA_KEYS}
        if isinstance(cleaned.get("output_config"), dict):
            cleaned["output_config"] = {key: value for key, value in
                                        cast(dict[str, Any], cleaned["output_config"]).items()
                                        if key != "effort"}
            if not cleaned["output_config"]:
                cleaned.pop("output_config")
        result["extra_body"] = cleaned
    parameters = selection.parameters.settings()
    if "extra_body" in parameters:
        result["extra_body"] = {**result.get("extra_body", {}), **parameters.pop("extra_body")}
    result.update(parameters)
    budget = selection.parameters.anthropic_thinking
    cap = result.get("max_tokens")
    if budget is not None and budget.budget_tokens is not None and isinstance(cap, int):
        if budget.budget_tokens + 1024 > cap:
            raise ValueError(
                "saved thinking budget exceeds max_tokens; choose a lower effort or change the cap"
            )
    return result
