import pytest
from pydantic_ai.messages import BinaryContent, TextContent, UserPromptPart
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.models.test import TestModel

from lumen.config import ModelSettingsConfig
from lumen.models import build_model


def test_build_test_model() -> None:
    assert isinstance(build_model(ModelSettingsConfig(id="test")), TestModel)


def test_openai_compatible_custom_base_url_defaults_to_responses() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:local-model",
            api_key="token",
            base_url="http://localhost:9000/v1",
        )
    )

    assert isinstance(model, OpenAIResponsesModel)
    assert model.model_name == "local-model"


def test_omlx_qwen_responses_configuration() -> None:
    """oMLX must use the OpenAI provider so the Responses Adapter is selected."""

    model = build_model(
        ModelSettingsConfig(
            id="openai:Qwen3.8-27B-4bit",
            api_key="local-test-token",
            base_url="http://127.0.0.1:8091/v1",
            api="responses",
        )
    )

    assert isinstance(model, OpenAIResponsesModel)
    assert model.model_name == "Qwen3.8-27B-4bit"


def test_model_settings_default_api_is_responses() -> None:
    assert ModelSettingsConfig(id="openai:local-model").api == "responses"


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
            id="openai:deepseek-v4-flash",
            api_key="token",
            base_url="https://api.deepseek.com/v1",
            api="openai-completions",
        )
    )
    assert isinstance(model, OpenAIChatModel)


def test_api_alias_chat_completions_maps_to_chat() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:deepseek-v4-flash",
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


def test_explicit_api_responses_with_custom_base_url() -> None:
    """Custom Responses-compatible endpoints retain the Responses Adapter."""

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


def test_openai_default_without_base_url_uses_responses() -> None:
    """The official OpenAI endpoint uses the same Responses default."""

    model = build_model(ModelSettingsConfig(id="openai:gpt-5", api_key="token"))
    assert isinstance(model, OpenAIResponsesModel)


def test_explicit_none_api_uses_responses_for_legacy_generated_configs() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:local-model",
            api_key="token",
            base_url="http://localhost:9000/v1",
            api=None,
        )
    )
    assert isinstance(model, OpenAIResponsesModel)


async def test_responses_adapter_maps_neutral_image_to_input_image() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:vision-model",
            api_key="token",
            base_url="http://localhost:9000/v1",
            api="responses",
            input_modalities=("text", "image"),
        )
    )
    assert isinstance(model, OpenAIResponsesModel)
    mapped = await model._map_user_prompt(  # type: ignore[reportPrivateUsage]
        UserPromptPart(
            [
                TextContent("inspect"),
                BinaryContent(b"\x89PNG\r\n\x1a\nimage", media_type="image/png"),
            ]
        )
    )

    assert mapped["content"][1]["type"] == "input_image"  # type: ignore[index]
    assert mapped["content"][1]["image_url"].startswith("data:image/png;base64,")  # type: ignore[index,union-attr]


async def test_chat_adapter_maps_neutral_image_to_image_url_content() -> None:
    model = build_model(
        ModelSettingsConfig(
            id="openai:vision-model",
            api_key="token",
            base_url="http://localhost:9000/v1",
            api="chat",
            input_modalities=("text", "image"),
        )
    )
    assert isinstance(model, OpenAIChatModel)
    mapped = await model._map_user_prompt(  # type: ignore[reportPrivateUsage]
        UserPromptPart(
            [
                TextContent("inspect"),
                BinaryContent(b"\x89PNG\r\n\x1a\nimage", media_type="image/png"),
            ]
        )
    )

    assert mapped["content"][1]["type"] == "image_url"  # type: ignore[index]
    assert mapped["content"][1]["image_url"]["url"].startswith(  # type: ignore[index]
        "data:image/png;base64,"
    )
