from __future__ import annotations

from typing import Any, cast

from openai.types import responses
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from lumen.agent_loop import ModelNativeTool
from lumen.config import ModelSettingsConfig
from lumen.provider_catalog import (
    ModelProtocol,
    effective_base_url,
    find_native_web_search_rule,
    model_protocol,
    supports_native_web_search,
)


class _KimiResponsesModel(OpenAIResponsesModel):
    """Kimi wire quirks; remove these overrides when its endpoint accepts SDK defaults."""

    async def _map_user_prompt(self, part: UserPromptPart) -> responses.EasyInputMessageParam:
        message = await super()._map_user_prompt(part)
        # Kimi accepts a bare content string, but does not reliably execute
        # native search for that form. Content parts preserve search routing
        # as well as mixed text/image input. Do not mutate canonical history.
        if isinstance(content := message["content"], str):
            message["content"] = [{"type": "input_text", "text": content}]
        return message

    def _get_native_tools(
        self,
        model_request_parameters: ModelRequestParameters,
    ) -> list[responses.ToolParam]:
        tools = super()._get_native_tools(model_request_parameters)
        for tool in tools:
            if tool.get("type") in {"web_search", "web_search_preview"}:
                cast(dict[str, Any], tool).pop("search_context_size", None)
        return tools


def native_web_search_enabled(config: ModelSettingsConfig) -> bool:
    """Resolve the model-level policy without guessing unknown provider capabilities."""

    if config.native_web_search.mode == "disabled":
        return False
    if config.native_web_search.mode == "enabled":
        return True
    return supports_native_web_search(config.id, config.api, config.base_url)


def build_native_tools(config: ModelSettingsConfig) -> tuple[ModelNativeTool, ...]:
    if not native_web_search_enabled(config):
        return ()
    return (
        ModelNativeTool(
            kind="web_search",
            search_context_size=config.native_web_search.search_context_size,
        ),
    )


def build_model(config: ModelSettingsConfig) -> Model | str:
    """Build a Pydantic AI model while honoring non-standard key variables and endpoints.

    OpenAI-compatible providers (DeepSeek, 阿里云百炼, Ollama, Azure-OpenAI proxies)
    accept an explicit ``api`` selector:

    * ``chat`` → ``OpenAIChatModel`` (the ``/chat/completions`` endpoint).
    * ``responses`` → ``OpenAIResponsesModel`` (the ``/responses`` endpoint,
      used by 阿里云百炼's Responses-compatible API and OpenAI's Responses API).

    When ``api`` is unset, Responses is the default for both OpenAI and custom
    OpenAI-compatible endpoints. Providers that only implement Chat Completions
    remain supported through the explicit ``api: chat`` compatibility Adapter.
    """
    if config.id == "test":
        return TestModel()
    try:
        provider_name, model_name = config.id.split(":", 1)
    except ValueError as error:
        raise ValueError("model id must use the '<provider>:<model>' format") from error

    if provider_name == "openai":
        provider = OpenAIProvider(
            api_key=config.api_key, base_url=effective_base_url(config.id, config.base_url),
        )
        api_choice = model_protocol(config.id, config.api)
        if api_choice is ModelProtocol.RESPONSES:
            native_search = find_native_web_search_rule(config.id, config.api, config.base_url)
            model_type = (
                OpenAIResponsesModel
                if native_search is None or native_search.vendor != "kimi-coding"
                else _KimiResponsesModel
            )
            return model_type(model_name, provider=provider)
        if api_choice is ModelProtocol.CHAT:
            return OpenAIChatModel(model_name, provider=provider)
        # ModelSettingsConfig validates known values, so this is only defensive
        # for callers that bypass normal validation.
        raise ValueError(f"unsupported OpenAI-compatible api selector: {config.api!r}")
    if provider_name == "ollama":
        provider = OllamaProvider(base_url=config.base_url, api_key=config.api_key)
        return OpenAIChatModel(model_name, provider=provider)
    if provider_name == "anthropic":
        provider = AnthropicProvider(
            api_key=config.api_key, base_url=effective_base_url(config.id, config.base_url),
        )
        return AnthropicModel(model_name, provider=provider)
    if provider_name in {"google", "gemini"}:
        if config.api_key is None:
            if config.base_url is not None:
                raise ValueError("a Google base_url requires api_key_env")
            return config.id
        provider = GoogleProvider(
            api_key=config.api_key, base_url=effective_base_url(config.id, config.base_url),
        )
        return GoogleModel(model_name, provider=provider)

    if config.api_key is not None or config.base_url is not None:
        raise ValueError(f"custom credentials/endpoints are not supported for provider {provider_name!r}")
    return config.id
