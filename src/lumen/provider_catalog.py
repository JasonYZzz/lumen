"""Reviewed provider contracts. No network discovery or SDK name heuristics.

Update RULES and its evidence when a vendor changes a model, then regenerate
docs/generated/provider-reasoning.md with scripts/export_provider_catalog.py.
Only exact model IDs match. Protocol compatibility is not provider identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit

CATALOG_REVISION = "2026-09-09.2"


class ReasoningLevel(StrEnum):
    PROVIDER_DEFAULT = "provider_default"
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ModelProtocol(StrEnum):
    RESPONSES = "responses"
    CHAT = "chat"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OTHER = "other"


class ReasoningCodec(StrEnum):
    OPENAI = "openai_effort"
    DEEPSEEK_CHAT = "deepseek_chat"
    ANTHROPIC_EFFORT = "anthropic_effort"
    ANTHROPIC_BUDGET = "anthropic_budget"
    GOOGLE_LEVEL = "google_level"
    NONE = "none"


def model_protocol(model_id: str, api: str | None) -> ModelProtocol:
    prefix = model_id.partition(":")[0]
    if prefix == "openai":
        if api in {None, "responses", "openai-responses"}:
            return ModelProtocol.RESPONSES
        if api in {"chat", "openai-completions", "chat-completions"}:
            return ModelProtocol.CHAT
        raise ValueError(f"unsupported OpenAI-compatible api selector: {api!r}")
    return {"anthropic": ModelProtocol.ANTHROPIC, "google": ModelProtocol.GOOGLE,
            "gemini": ModelProtocol.GOOGLE, "ollama": ModelProtocol.CHAT}.get(prefix, ModelProtocol.OTHER)


def effective_base_url(model_id: str, configured: str | None) -> str | None:
    """Use the same endpoint precedence as the SDK, including explicit env overrides."""
    if configured is not None:
        return configured
    prefix = model_id.partition(":")[0]
    if prefix == "openai":
        return os.environ.get("OPENAI_BASE_URL")
    if prefix == "anthropic":
        return os.environ.get("ANTHROPIC_BASE_URL")
    if prefix in {"google", "gemini"}:
        return os.environ.get("GOOGLE_GEMINI_BASE_URL")
    return None


@dataclass(frozen=True)
class ProviderEndpoint:
    vendor: str
    protocols: tuple[ModelProtocol, ...]
    hosts: tuple[str, ...]
    paths: tuple[str, ...]
    host_suffix: str | None = None

    def matches(self, protocol: ModelProtocol, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            return (protocol in self.protocols and parsed.scheme == "https"
                    and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment
                    and (host in self.hosts or bool(self.host_suffix and host.endswith(self.host_suffix)))
                    and parsed.path.rstrip("/") in self.paths)
        except ValueError:
            return False


P = ModelProtocol
C = ReasoningCodec
L = ReasoningLevel
OPENAI_PROTOCOLS = (P.RESPONSES, P.CHAT)
ENDPOINTS = (
    ProviderEndpoint("openai", OPENAI_PROTOCOLS, ("api.openai.com",), ("/v1",)),
    ProviderEndpoint("anthropic", (P.ANTHROPIC,), ("api.anthropic.com",), ("",)),
    ProviderEndpoint("google", (P.GOOGLE,), ("generativelanguage.googleapis.com",), ("",)),
    ProviderEndpoint("deepseek", OPENAI_PROTOCOLS, ("api.deepseek.com",), ("", "/v1")),
    ProviderEndpoint("deepseek", (P.ANTHROPIC,), ("api.deepseek.com",), ("/anthropic",)),
    ProviderEndpoint("kimi-coding", OPENAI_PROTOCOLS, ("api.kimi.com",), ("/coding/v1",)),
    ProviderEndpoint("kimi-coding", (P.ANTHROPIC,), ("api.kimi.com",), ("/coding",)),
    ProviderEndpoint("moonshot", (P.CHAT,), ("api.moonshot.cn", "api.moonshot.ai"), ("/v1",)),
    ProviderEndpoint("bailian", (P.ANTHROPIC,),
                     ("dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"), ("/apps/anthropic",),
                     ".maas.aliyuncs.com"),
)


@dataclass(frozen=True)
class ModelReasoningRule:
    key: str
    vendor: str
    models: tuple[str, ...]
    protocols: tuple[ModelProtocol, ...]
    codec: ReasoningCodec
    levels: tuple[ReasoningLevel, ...]
    sources: tuple[str, ...]
    aliases: tuple[tuple[ReasoningLevel, ReasoningLevel], ...] = ()
    reviewed_on: str = "2026-09-08"
    note: str = ""
    default_level: ReasoningLevel | None = None

    def level_map(self) -> dict[ReasoningLevel, ReasoningLevel]:
        return {**{level: level for level in self.levels}, **dict(self.aliases)}


@dataclass(frozen=True)
class NativeWebSearchRule:
    """Reviewed provider-native web search contract."""

    vendor: str
    models: tuple[str, ...]
    protocols: tuple[ModelProtocol, ...]
    sources: tuple[str, ...]
    sends_search_context_size: bool = True
    reviewed_on: str = "2026-09-09"


DEEPSEEK_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
BAILIAN_DEEPSEEK_MODELS = (*DEEPSEEK_MODELS, "deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.2")
DS_RESPONSES = "https://api-docs.deepseek.com/api/create-response/"
DS_RESPONSES_GUIDE = "https://api-docs.deepseek.com/guides/responses_api/"
DS_CHAT = "https://api-docs.deepseek.com/api/create-chat-completion"
DS_ANTHROPIC = "https://api-docs.deepseek.com/guides/anthropic_api/"
DS_THINKING = "https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/"
BAILIAN = "https://help.aliyun.com/zh/model-studio/anthropic-api-messages"
BAILIAN_WEB_SEARCH = "https://help.aliyun.com/zh/model-studio/web-search"
KIMI = "https://www.kimi.com/code/docs/en/kimi-code/models.html"
MOONSHOT = "https://platform.kimi.com/docs/guide/kimi-k3-quickstart"
LMH = (L.LOW, L.MEDIUM, L.HIGH)
DS_LEVELS = (L.OFF, L.LOW, L.HIGH, L.MAX)
DS_ALIASES = ((L.MEDIUM, L.HIGH), (L.XHIGH, L.HIGH))

RULES = (
    ModelReasoningRule("deepseek-v4-responses", "deepseek", DEEPSEEK_MODELS, (P.RESPONSES,),
                       C.OPENAI, DS_LEVELS, (DS_RESPONSES, DS_THINKING),
                       (*DS_ALIASES, (L.MINIMAL, L.LOW)), default_level=L.HIGH),
    ModelReasoningRule("deepseek-v4-chat", "deepseek", DEEPSEEK_MODELS, (P.CHAT,),
                       C.DEEPSEEK_CHAT, DS_LEVELS, (DS_CHAT, DS_THINKING), DS_ALIASES, default_level=L.HIGH),
    ModelReasoningRule("deepseek-v4-anthropic", "deepseek", DEEPSEEK_MODELS, (P.ANTHROPIC,),
                       C.ANTHROPIC_EFFORT, DS_LEVELS, (DS_ANTHROPIC, DS_THINKING), DS_ALIASES,
                       default_level=L.HIGH),
    ModelReasoningRule("bailian-qwen38", "bailian", ("qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash"),
                       (P.ANTHROPIC,), C.ANTHROPIC_EFFORT, (L.OFF, L.LOW, L.MEDIUM, L.XHIGH),
                       (BAILIAN,), ((L.HIGH, L.XHIGH), (L.MAX, L.XHIGH)), default_level=L.XHIGH),
    ModelReasoningRule("bailian-deepseek-glm", "bailian", BAILIAN_DEEPSEEK_MODELS, (P.ANTHROPIC,),
                       C.ANTHROPIC_EFFORT, (L.OFF, L.HIGH, L.MAX), (BAILIAN,),
                       ((L.LOW, L.HIGH), (L.MEDIUM, L.HIGH), (L.XHIGH, L.MAX)), default_level=L.MAX),
    ModelReasoningRule("kimi-coding-openai", "kimi-coding", ("k3", "k3-256k"), OPENAI_PROTOCOLS,
                       C.OPENAI, (L.LOW, L.HIGH, L.MAX), (KIMI,), ((L.MEDIUM, L.HIGH), (L.XHIGH, L.MAX)),
                       note="off routes to K2.6 and is deliberately unavailable.", default_level=L.HIGH),
    ModelReasoningRule("kimi-coding-anthropic", "kimi-coding", ("k3", "k3-256k"), (P.ANTHROPIC,),
                       C.ANTHROPIC_EFFORT, (L.LOW, L.HIGH, L.MAX), (KIMI,),
                       ((L.MEDIUM, L.HIGH), (L.XHIGH, L.MAX)),
                       note="off routes to K2.6.", default_level=L.HIGH),
    ModelReasoningRule("moonshot-k3", "moonshot", ("kimi-k3",), (P.CHAT,), C.OPENAI,
                       (L.LOW, L.HIGH, L.MAX), (MOONSHOT,), default_level=L.MAX),
    ModelReasoningRule("openai-gpt56-sol", "openai", ("gpt-5.6", "gpt-5.6-sol"), OPENAI_PROTOCOLS,
                       C.OPENAI, (L.OFF, *LMH, L.XHIGH, L.MAX),
                       ("https://developers.openai.com/api/docs/models/gpt-5.6-sol",),
                       default_level=L.MEDIUM),
    ModelReasoningRule("openai-gpt56-terra", "openai", ("gpt-5.6-terra",), OPENAI_PROTOCOLS,
                       C.OPENAI, (L.OFF, *LMH, L.XHIGH, L.MAX),
                       ("https://developers.openai.com/api/docs/models/gpt-5.6-terra",),
                       default_level=L.MEDIUM),
    ModelReasoningRule("openai-gpt56-luna", "openai", ("gpt-5.6-luna",), OPENAI_PROTOCOLS,
                       C.OPENAI, (L.OFF, *LMH, L.XHIGH, L.MAX),
                       ("https://developers.openai.com/api/docs/models/gpt-5.6-luna",),
                       default_level=L.MEDIUM),
    ModelReasoningRule("openai-gpt6-astra", "openai", ("gpt-6-astra",), OPENAI_PROTOCOLS,
                       C.OPENAI, (*LMH, L.XHIGH, L.MAX),
                       ("https://developers.openai.com/api/docs/models/gpt-6-astra",
                        "https://developers.openai.com/api/docs/guides/latest-model"),
                       note="No off/minimal. Tool calling requires Responses; Chat supports text requests."),
)

NATIVE_WEB_SEARCH_RULES = (
    NativeWebSearchRule(
        vendor="deepseek",
        models=DEEPSEEK_MODELS,
        protocols=(P.RESPONSES,),
        sources=(DS_RESPONSES_GUIDE,),
    ),
    NativeWebSearchRule(
        vendor="kimi-coding",
        models=("k3",),
        protocols=(P.RESPONSES,),
        sources=(KIMI,),
        # Kimi's Responses endpoint accepts {"type": "web_search"} but
        # rejects the optional OpenAI search_context_size extension.
        sends_search_context_size=False,
    ),
    NativeWebSearchRule(
        vendor="bailian",
        models=("qwen3.8-max", "qwen3.8-flash"),
        protocols=(P.ANTHROPIC,),
        sources=(BAILIAN_WEB_SEARCH,),
    ),
)


def provider_vendor(model_id: str, api: str | None, base_url: str | None) -> str | None:
    protocol = model_protocol(model_id, api)
    url = effective_base_url(model_id, base_url)
    if url is None:
        return {"openai": "openai", "anthropic": "anthropic", "google": "google", "gemini": "google"}.get(
            model_id.partition(":")[0],
        )
    vendors = {endpoint.vendor for endpoint in ENDPOINTS if endpoint.matches(protocol, url)}
    if len(vendors) > 1:
        raise ValueError("ambiguous provider endpoint contract")
    return next(iter(vendors), None)


def find_reasoning_rule(model_id: str, api: str | None, base_url: str | None) -> ModelReasoningRule | None:
    protocol = model_protocol(model_id, api)
    vendor = provider_vendor(model_id, api, base_url)
    name = model_id.partition(":")[2]
    matches = [rule for rule in RULES if rule.vendor == vendor and protocol in rule.protocols
               and name in rule.models]
    if len(matches) > 1:
        raise ValueError("ambiguous model reasoning contract")
    return matches[0] if matches else None


def find_native_web_search_rule(
    model_id: str,
    api: str | None,
    base_url: str | None,
) -> NativeWebSearchRule | None:
    """Return the exact reviewed native-search contract for one model route."""

    protocol = model_protocol(model_id, api)
    vendor = provider_vendor(model_id, api, base_url)
    name = model_id.partition(":")[2]
    matches = [
        rule
        for rule in NATIVE_WEB_SEARCH_RULES
        if rule.vendor == vendor and protocol in rule.protocols and name in rule.models
    ]
    if len(matches) > 1:
        raise ValueError("ambiguous native web search contract")
    return matches[0] if matches else None


def supports_native_web_search(model_id: str, api: str | None, base_url: str | None) -> bool:
    """Return whether automatic native search is backed by an exact reviewed contract."""

    return find_native_web_search_rule(model_id, api, base_url) is not None


def validate_catalog() -> None:
    """Run on import and in CI: duplicates/invalid aliases never depend on table order."""
    keys: set[str] = set()
    identities: set[tuple[str, str, ModelProtocol]] = set()
    for rule in RULES:
        if rule.key in keys or not rule.sources or not rule.models or not rule.protocols:
            raise ValueError(f"invalid catalog rule: {rule.key}")
        keys.add(rule.key)
        date.fromisoformat(rule.reviewed_on)
        for source in rule.sources:
            if urlsplit(source).scheme != "https":
                raise ValueError(f"invalid official source: {rule.key}")
        allowed_protocols = {
            C.OPENAI: OPENAI_PROTOCOLS, C.DEEPSEEK_CHAT: (P.CHAT,),
            C.ANTHROPIC_EFFORT: (P.ANTHROPIC,), C.ANTHROPIC_BUDGET: (P.ANTHROPIC,),
            C.GOOGLE_LEVEL: (P.GOOGLE,), C.NONE: tuple(P),
        }
        if not set(rule.protocols) <= set(allowed_protocols[rule.codec]):
            raise ValueError(f"codec/protocol mismatch: {rule.key}")
        if (rule.codec is C.NONE) != (not rule.levels):
            raise ValueError(f"codec/levels mismatch: {rule.key}")
        if len(set(rule.levels)) != len(rule.levels) or L.PROVIDER_DEFAULT in rule.levels:
            raise ValueError(f"invalid native levels: {rule.key}")
        if rule.default_level is not None and rule.default_level not in rule.levels:
            raise ValueError(f"invalid provider default: {rule.key}")
        seen = set(rule.levels)
        for alias, target in rule.aliases:
            if alias in seen or alias in {L.OFF, L.PROVIDER_DEFAULT} or target not in rule.levels:
                raise ValueError(f"invalid alias: {rule.key}")
            seen.add(alias)
        for name in rule.models:
            for protocol in rule.protocols:
                identity = (rule.vendor, name, protocol)
                if identity in identities:
                    raise ValueError(f"duplicate model contract: {identity}")
                identities.add(identity)
    native_identities: set[tuple[str, str, ModelProtocol]] = set()
    for rule in NATIVE_WEB_SEARCH_RULES:
        if not rule.models or not rule.protocols or not rule.sources:
            raise ValueError(f"invalid native web search rule: {rule.vendor}")
        date.fromisoformat(rule.reviewed_on)
        if any(urlsplit(source).scheme != "https" for source in rule.sources):
            raise ValueError(f"invalid native web search source: {rule.vendor}")
        for name in rule.models:
            for protocol in rule.protocols:
                identity = (rule.vendor, name, protocol)
                if identity in native_identities:
                    raise ValueError(f"duplicate native web search contract: {identity}")
                native_identities.add(identity)


validate_catalog()
