import pytest
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from lumen.config import ModelSettingsConfig
from lumen.models import build_model


def test_build_test_model() -> None:
    assert isinstance(build_model(ModelSettingsConfig(id="test")), TestModel)


def test_build_openai_compatible_model_with_custom_base_url() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:local-model",
            api_key="token",
            base_url="http://localhost:9000/v1",
        )
    )

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "local-model"


def test_explicit_api_chat_returns_chat_model() -> None:
    """Aliyun DashScope via the /chat/completions path."""

    model = build_model(
        ModelSettingsConfig(
            id="openai:glm-5.2",
            api_key="token",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api="chat",
        )
    )
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "glm-5.2"


def test_explicit_api_responses_returns_responses_model() -> None:
    """阿里云 Responses API via the /responses path."""

    model = build_model(
        ModelSettingsConfig(
            id="openai:qwen3-max",
            api_key="token",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api="responses",
        )
    )
    assert isinstance(model, OpenAIResponsesModel)


def test_api_alias_openai_completions_maps_to_chat() -> None:
    """Configs pasted from external tools (Roo Code etc.) use this alias."""

    model = build_model(
        ModelSettingsConfig(
            id="openai:deepseek-v4-pro",
            api_key="token",
            base_url="https://api.deepseek.com/v1",
            api="openai-completions",
        )
    )
    assert isinstance(model, OpenAIChatModel)


def test_api_alias_chat_completions_maps_to_chat() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:deepseek-v4-pro",
            api_key="token",
            base_url="https://api.deepseek.com/v1",
            api="chat-completions",
        )
    )
    assert isinstance(model, OpenAIChatModel)


def test_api_alias_openai_responses_maps_to_responses() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:qwen3-max",
            api_key="token",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api="openai-responses",
        )
    )
    assert isinstance(model, OpenAIResponsesModel)


def test_explicit_api_responses_overrides_base_url_default() -> None:
    """Even with a custom base_url, api=responses wins (lets users pick the
    Responses path on Aliyun instead of the legacy chat default)."""

    model = build_model(
        ModelSettingsConfig(
            id="openai:glm-5.2",
            api_key="token",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api="responses",
        )
    )
    assert isinstance(model, OpenAIResponsesModel)


def test_invalid_api_value_rejected_at_config_parse() -> None:
    """Unknown api values fail at config validation, not at model build."""

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelSettingsConfig(
            id="openai:glm-5.2",
            api_key="token",
            api="bogus",  # type: ignore[arg-type]
        )


def test_openai_default_without_base_url_keeps_responses() -> None:
    """No base_url + no api → responses (preserves historical behaviour)."""

    model = build_model(ModelSettingsConfig(id="openai:gpt-5", api_key="token"))
    assert isinstance(model, OpenAIResponsesModel)
