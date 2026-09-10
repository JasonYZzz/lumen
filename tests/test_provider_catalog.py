from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from test_pydantic_driver import _request  # pyright: ignore[reportPrivateUsage]

from lumen.agent_loop import PydanticAIModelDriver
from lumen.config import ModelSettingsConfig
from lumen.configuration import WorkspaceConfiguration
from lumen.models import build_model
from lumen.provider_catalog import (
    CATALOG_REVISION,
    ENDPOINTS,
    RULES,
    ModelProtocol,
    ModelReasoningRule,
    ReasoningCodec,
    ReasoningLevel,
    find_native_web_search_rule,
    find_reasoning_rule,
    supports_native_web_search,
    validate_catalog,
)
from lumen.reasoning import apply_reasoning, resolve_reasoning

L = ReasoningLevel
P = ModelProtocol
C = ReasoningCodec


def config_for(rule: ModelReasoningRule, protocol: ModelProtocol, model: str) -> ModelSettingsConfig:
    endpoint = next(item for item in ENDPOINTS if item.vendor == rule.vendor and protocol in item.protocols)
    prefix = {P.CHAT: "openai", P.RESPONSES: "openai", P.ANTHROPIC: "anthropic", P.GOOGLE: "google"}[protocol]
    return ModelSettingsConfig.model_validate({
        "id": f"{prefix}:{model}", "base_url": "https://" + endpoint.hosts[0] + endpoint.paths[0],
        "api": protocol.value if protocol in {P.CHAT, P.RESPONSES} else None,
    })


def test_all_catalog_models_resolve_unambiguously_and_reject_unpublished_levels() -> None:
    validate_catalog()
    for rule in RULES:
        assert date.fromisoformat(rule.reviewed_on)
        assert all(source.startswith("https://") for source in rule.sources)
        for model in rule.models:
            for protocol in rule.protocols:
                config = config_for(rule, protocol, model)
                selection = resolve_reasoning(config)
                assert selection.catalog_revision == CATALOG_REVISION
                assert selection.provider == rule.vendor
                assert selection.capability_documents == rule.sources
                assert selection.requested is None
                assert selection.parameters.settings() == {}
                for level in set(L) - set(selection.supported_levels):
                    with pytest.raises(ValueError):
                        resolve_reasoning(config, level)


@pytest.mark.parametrize(("model", "levels"), [
    ("openai:gpt-5.6", (L.OFF, L.LOW, L.MEDIUM, L.HIGH, L.XHIGH, L.MAX)),
    ("openai:gpt-6-astra", (L.LOW, L.MEDIUM, L.HIGH, L.XHIGH, L.MAX)),
])
def test_documented_model_differences_are_preserved(model: str, levels: tuple[L, ...]) -> None:
    assert resolve_reasoning(ModelSettingsConfig(id=model)).supported_levels == (L.PROVIDER_DEFAULT, *levels)


@pytest.mark.parametrize(("model", "url"), [
    ("openai:gpt-5.6", "https://proxy.example/v1"),
    ("openai:gpt-5.6", "https://api.openai.com.attacker.example/v1"),
    ("openai:gpt-5.6", "https://api.openai.com:8443/v1"),
    ("openai:gpt-5.6", "https://api.openai.com/another-app"),
    ("anthropic:qwen3.8-max", "https://unrelated.aliyuncs.com/apps/anthropic"),
    ("anthropic:claude-opus-4-6", "https://api.deepseek.com/anthropic"),
    ("openai:k3", "https://api.kimi.com/v1"),
    ("openai:gpt-5.99", None),
    ("anthropic:claude-opus-99", None),
    ("google:gemini-99-pro", None),
])
def test_unknown_versions_and_endpoints_never_inherit_sdk_name_guesses(model: str, url: str | None) -> None:
    selection = resolve_reasoning(ModelSettingsConfig(id=model, base_url=url))
    assert selection.capability_status == "unknown"
    assert selection.supported_levels == (L.PROVIDER_DEFAULT,)
    assert selection.catalog_revision is None


@pytest.mark.parametrize(("model", "api", "url"), [
    ("openai:deepseek-v4-flash", "responses", "https://api.deepseek.com"),
    ("openai:deepseek-v4-pro", "responses", "https://api.deepseek.com"),
    ("openai:k3", "responses", "https://api.kimi.com/coding/v1"),
    (
        "anthropic:qwen3.8-max",
        None,
        "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
    ),
    (
        "anthropic:qwen3.8-flash",
        None,
        "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
    ),
])
def test_every_current_project_model_has_reviewed_native_web_search(
    model: str,
    api: str | None,
    url: str,
) -> None:
    assert supports_native_web_search(model, api, url)
    assert find_native_web_search_rule(model, api, url) is not None


def test_native_web_search_never_leaks_across_endpoints_or_protocols() -> None:
    assert not supports_native_web_search("openai:k3", "responses", "https://api.kimi.com/v1")
    assert not supports_native_web_search("openai:k3", "chat", "https://api.kimi.com/coding/v1")
    assert not supports_native_web_search(
        "anthropic:qwen3.8-max",
        None,
        "https://unrelated.aliyuncs.com/apps/anthropic",
    )


def test_proxy_profile_is_explicit_and_cannot_override_vendor_or_model() -> None:
    config = ModelSettingsConfig(id="openai:gpt-5.6", base_url="https://proxy.example/v1",
                                reasoning_profile="openai-gpt56-sol", reasoning_effort=L.MAX)
    selection = resolve_reasoning(config)
    assert selection.capability_source == "deployment_profile:openai-gpt56-sol"
    assert selection.parameters.openai_reasoning_effort == "max"
    assert ModelSettingsConfig.model_validate(config.model_dump()).reasoning_profile == "openai-gpt56-sol"
    for changes in (
        {"id": "openai:typo"}, {"reasoning_profile": "future"},
        {"base_url": "https://api.deepseek.com"},
        {"id": "anthropic:claude-sonnet-4-6"},
    ):
        with pytest.raises(ValueError):
            ModelSettingsConfig.model_validate({**config.model_dump(), **changes})


def test_known_capabilities_can_only_be_narrowed() -> None:
    for model, level in (("openai:gpt-6-astra", L.OFF), ("openai:gpt-6-astra", L.MINIMAL),
                         ("openai:gpt-5.6", L.MINIMAL)):
        with pytest.raises(ValueError, match="unsupported"):
            ModelSettingsConfig(id=model, reasoning_levels=(level,))
    selection = resolve_reasoning(ModelSettingsConfig(id="openai:gpt-5.6", reasoning_levels=(L.LOW,)))
    assert selection.supported_levels == (L.PROVIDER_DEFAULT, L.LOW)


def test_documented_defaults_are_metadata_and_never_turn_into_explicit_effort() -> None:
    expected = {
        "deepseek-v4-responses": L.HIGH, "deepseek-v4-chat": L.HIGH, "deepseek-v4-anthropic": L.HIGH,
        "bailian-qwen38": L.XHIGH, "bailian-deepseek-glm": L.MAX,
        "kimi-coding-openai": L.HIGH, "kimi-coding-anthropic": L.HIGH, "moonshot-k3": L.MAX,
        "openai-gpt56-sol": L.MEDIUM, "openai-gpt56-terra": L.MEDIUM, "openai-gpt56-luna": L.MEDIUM,
        "openai-gpt6-astra": None,
    }
    for rule in RULES:
        config = config_for(rule, rule.protocols[0], rule.models[0])
        for choice in (None, L.PROVIDER_DEFAULT):
            selection = resolve_reasoning(config, choice)
            assert selection.provider_default_level == expected[rule.key]
            assert selection.parameters.settings() == {}
            assert selection.effective is None
        proxy = config.model_copy(update={"base_url": "https://proxy.example/v1",
                                          "reasoning_profile": rule.key})
        assert resolve_reasoning(proxy).provider_default_level is None
    chosen = resolve_reasoning(ModelSettingsConfig(id="openai:gpt-5.6"), L.LOW)
    assert chosen.effective is L.LOW
    assert chosen.provider_default_level is L.MEDIUM


def test_curated_catalog_excludes_removed_families_and_profiles() -> None:
    assert {name for rule in RULES if rule.vendor == "openai" for name in rule.models} == {
        "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra",
    }
    assert not any(rule.vendor in {"anthropic", "google"} for rule in RULES)
    for model, profile in (
        ("openai:gpt-5", "openai-gpt5"), ("openai:gpt-5.4", "openai-gpt54"),
        ("openai:gpt-4o", "openai-gpt4o"),
        ("anthropic:claude-opus-4-6", "claude-adaptive46"),
        ("google:gemini-3-flash-preview", "gemini3-flash"),
    ):
        config = ModelSettingsConfig(id=model)
        selection = resolve_reasoning(config)
        assert selection.capability_status == "unknown"
        assert selection.supported_levels == (L.PROVIDER_DEFAULT,)
        with pytest.raises(ValueError, match="declare"):
            resolve_reasoning(config, L.LOW)
        with pytest.raises(ValueError, match="unknown reasoning_profile"):
            ModelSettingsConfig(id=model, reasoning_profile=profile)


def test_managed_configuration_round_trips_profile_without_editing_original(tmp_path: Path) -> None:
    original = tmp_path / ".lumen/agent.yaml"
    original.parent.mkdir()
    text = "version: 2\nagent:\n  models:\n    first: {id: test}\n  default_model: first\n"
    original.write_text(text)
    configuration = WorkspaceConfiguration(tmp_path, home=tmp_path / "home", environ={})
    before = configuration.inspect()
    saved = configuration.upsert_model(
        expected_revision=before.revision, name="proxy", set_default=False, definition={
            "id": "openai:gpt-5.6", "base_url": "https://proxy.example/v1",
            "reasoning_profile": "openai-gpt56-sol", "reasoning_effort": "max",
        },
    )
    model = next(item for item in saved.models if item.name == "proxy")
    assert model.reasoning_profile == "openai-gpt56-sol"
    assert model.reasoning_effort == "max"
    assert original.read_text() == text
    reopened = WorkspaceConfiguration(tmp_path, home=tmp_path / "home", environ={}).inspect()
    restored = next(item for item in reopened.models if item.name == "proxy")
    assert restored.reasoning_profile == model.reasoning_profile


def test_managed_mcp_toggle_writes_only_activation_policy(tmp_path: Path) -> None:
    original = tmp_path / ".lumen/agent.yaml"
    original.parent.mkdir()
    original.write_text(
        """version: 2
agent: {model: {id: test}}
mcp_servers:
  exa:
    transport: streamable_http
    url: https://mcp.exa.ai/mcp
    headers: {x-api-key: "${EXA_API_KEY:-secret-value}"}
"""
    )
    configuration = WorkspaceConfiguration(
        tmp_path,
        home=tmp_path / "home",
        environ={},
    )
    before = configuration.inspect()

    saved = configuration.set_mcp_server_enabled(
        expected_revision=before.revision,
        name="exa",
        enabled=False,
    )

    assert saved.mcp_servers[0].enabled is False
    managed = (tmp_path / ".lumen/agent.web.yaml").read_text()
    assert "secret-value" not in managed
    assert "mcp_servers" not in managed
    assert "enabled:" in managed and "exa: false" in managed


def test_endpoint_environment_and_model_builder_share_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    config = ModelSettingsConfig(id="openai:gpt-5.6", api_key="test")
    assert resolve_reasoning(config).capability_status == "unknown"
    model = build_model(config)
    assert isinstance(model, OpenAIResponsesModel)
    assert model.base_url == "https://proxy.example/v1/"
    config.base_url = "https://api.openai.com/v1"
    assert resolve_reasoning(config).capability_status == "supported"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    selection = resolve_reasoning(ModelSettingsConfig(id="anthropic:claude-opus-4-6"))
    assert selection.capability_status == "unknown"


WIRE_CASES = [(rule, protocol, level) for rule in RULES for protocol in rule.protocols
              for level in (*rule.level_map(), L.PROVIDER_DEFAULT)]


@pytest.mark.parametrize(("rule", "protocol", "level"), WIRE_CASES,
                         ids=[f"{rule.key}-{protocol}-{level}" for rule, protocol, level in WIRE_CASES])
async def test_every_catalog_mapping_serializes_through_the_real_sdk(
    rule: ModelReasoningRule, protocol: ModelProtocol, level: ReasoningLevel,
) -> None:
    config = config_for(rule, protocol, rule.models[0])
    selection = resolve_reasoning(config, level)
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "offline contract test", "code": 400}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if protocol is P.ANTHROPIC:
            model = AnthropicModel(rule.models[0], provider=AnthropicProvider(anthropic_client=AsyncAnthropic(
                api_key="test", http_client=client,
            )))
        elif protocol is P.GOOGLE:
            model = GoogleModel(rule.models[0], provider=GoogleProvider(api_key="test", http_client=client))
        else:
            model_class = OpenAIResponsesModel if protocol is P.RESPONSES else OpenAIChatModel
            model = model_class(rule.models[0], provider=OpenAIProvider(openai_client=AsyncOpenAI(
                api_key="test", http_client=client,
            )))
        async with PydanticAIModelDriver(model) as driver:
            request = replace(_request(), settings=apply_reasoning(config.settings, selection))
            async with driver.open_stream(request) as stream:
                _ = [event async for event in stream.events]
    assert len(bodies) == 1
    body = bodies[0]
    if level is L.PROVIDER_DEFAULT:
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        assert "effort" not in body.get("reasoning", {})
        assert "effort" not in body.get("output_config", {})
        return
    expected = rule.level_map()[level].value
    if protocol is P.GOOGLE:
        # ProtoJSON accepts original proto field names and lowerCamelCase.
        # https://protobuf.dev/programming-guides/json/#field-names
        thinking = body["generationConfig"]["thinkingConfig"]
        assert thinking.get("thinkingLevel", thinking.get("thinking_level")) == expected.upper()
        assert thinking.get("includeThoughts", thinking.get("include_thoughts")) is True
        assert "thinkingBudget" not in thinking and "thinking_budget" not in thinking
    elif protocol is P.ANTHROPIC:
        if level is L.OFF:
            assert body["thinking"] == {"type": "disabled"}
            assert "effort" not in body.get("output_config", {})
        else:
            if rule.codec is C.ANTHROPIC_BUDGET:
                assert body["thinking"]["budget_tokens"] == {
                    "minimal": 1024, "low": 2048, "medium": 10000, "high": 16384,
                }[expected]
            else:
                assert body["thinking"] == {"type": "enabled"}
            if rule.codec is not C.ANTHROPIC_BUDGET:
                assert body["output_config"]["effort"] == expected
    elif protocol is P.RESPONSES:
        assert body["reasoning"]["effort"] == ("none" if level is L.OFF else expected)
    elif rule.codec is C.DEEPSEEK_CHAT:
        assert body["thinking"] == {"type": "disabled" if level is L.OFF else "enabled"}
        assert body.get("reasoning_effort") == (None if level is L.OFF else expected)
    else:
        assert body["reasoning_effort"] == ("none" if level is L.OFF else expected)


def test_catalog_export_is_current() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.export_provider_catalog import render

    target = Path(__file__).resolve().parents[1] / "docs/generated/provider-reasoning.md"
    assert target.read_text() == render()


def test_duplicate_catalog_rules_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lumen.provider_catalog.RULES", (*RULES, RULES[0]))
    with pytest.raises(ValueError, match="invalid catalog rule"):
        validate_catalog()
    config = config_for(RULES[0], P.RESPONSES, "deepseek-v4-flash")
    with pytest.raises(ValueError, match="ambiguous model"):
        find_reasoning_rule(config.id, config.api, config.base_url)
