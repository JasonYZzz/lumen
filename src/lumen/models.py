from __future__ import annotations

from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from lumen.config import ModelSettingsConfig

# Aliases some configs (e.g. Roo Code) use. We normalise them to our two real
# paths so users can paste their existing config verbatim.
_API_ALIAS = {
    "openai-completions": "chat",
    "openai-responses": "responses",
    "chat-completions": "chat",
    "chat": "chat",
    "responses": "responses",
}


def build_model(config: ModelSettingsConfig) -> Model | str:
    """Build a Pydantic AI model while honoring non-standard key variables and endpoints.

    OpenAI-compatible providers (DeepSeek, 阿里云百炼, Ollama, Azure-OpenAI proxies)
    accept an explicit ``api`` selector:

    * ``chat`` → ``OpenAIChatModel`` (the ``/chat/completions`` endpoint).
    * ``responses`` → ``OpenAIResponsesModel`` (the ``/responses`` endpoint,
      used by 阿里云百炼's Responses-compatible API and OpenAI's Responses API).

    When ``api`` is unset we preserve the historical behaviour: a custom
    ``base_url`` implies Chat, the default OpenAI endpoint implies Responses.
    """
    if config.id == "test":
        return TestModel()
    try:
        provider_name, model_name = config.id.split(":", 1)
    except ValueError as error:
        raise ValueError("model id must use the '<provider>:<model>' format") from error

    if provider_name == "openai":
        provider = OpenAIProvider(api_key=config.api_key, base_url=config.base_url)
        api_choice = _API_ALIAS.get(config.api) if config.api is not None else None
        if api_choice == "responses":
            return OpenAIResponsesModel(model_name, provider=provider)
        if api_choice == "chat":
            return OpenAIChatModel(model_name, provider=provider)
        # No explicit selector: keep the legacy base_url-driven default.
        if config.base_url:
            return OpenAIChatModel(model_name, provider=provider)
        return OpenAIResponsesModel(model_name, provider=provider)
    if provider_name == "ollama":
        provider = OllamaProvider(base_url=config.base_url, api_key=config.api_key)
        return OpenAIChatModel(model_name, provider=provider)
    if provider_name == "anthropic":
        provider = AnthropicProvider(api_key=config.api_key, base_url=config.base_url)
        return AnthropicModel(model_name, provider=provider)
    if provider_name in {"google", "gemini"}:
        if config.api_key is None:
            if config.base_url is not None:
                raise ValueError("a Google base_url requires api_key_env")
            return config.id
        provider = GoogleProvider(api_key=config.api_key, base_url=config.base_url)
        return GoogleModel(model_name, provider=provider)

    if config.api_key is not None or config.base_url is not None:
        raise ValueError(f"custom credentials/endpoints are not supported for provider {provider_name!r}")
    return config.id
