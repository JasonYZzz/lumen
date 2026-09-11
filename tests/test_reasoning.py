from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from test_pydantic_driver import _request  # pyright: ignore[reportPrivateUsage]

from lumen.agent_loop import PydanticAIModelDriver
from lumen.agents.types import AgentExecutionResult, AgentMessage, AgentProfile, AgentThreadState
from lumen.application import (
    CancelRun,
    CreateSession,
    GetBootstrap,
    SelectModel,
    SelectReasoning,
    SetCollaborationMode,
    StartRun,
    WorkspaceBusyError,
    WorkspaceHost,
)
from lumen.collaboration import SessionSettingsState
from lumen.config import AgentsConfig, ModelSettingsConfig, load_config
from lumen.reasoning import ReasoningLevel as Level
from lumen.reasoning import ReasoningSelection, apply_reasoning, resolve_reasoning
from lumen.resources import ResourceManager
from lumen.sessions import SessionRepository
from lumen.ui.app import LumenApp
from lumen.ui.choice_picker import ChoicePickerScreen


def test_google_level_adapter_never_advertises_false_as_off() -> None:
    config = ModelSettingsConfig(id="google:custom-model", reasoning_levels=(Level.MINIMAL,))
    assert resolve_reasoning(config).capability_source == "deployment_declaration"
    assert Level.OFF not in resolve_reasoning(config).supported_levels
    with pytest.raises(ValueError):
        resolve_reasoning(config, Level.OFF)
    with pytest.raises(ValueError, match="only supports"):
        ModelSettingsConfig(id="google:custom-model", reasoning_levels=(Level.XHIGH,))
    assert resolve_reasoning(config, Level.MINIMAL).parameters.settings()["google_thinking_config"] == {
        "thinking_level": "MINIMAL", "include_thoughts": True,
    }


def test_configuration_distinguishes_omission_default_off_and_conflicts() -> None:
    omitted = ModelSettingsConfig(id="openai:gpt-6-astra")
    assert resolve_reasoning(omitted).requested is None
    assert resolve_reasoning(omitted, Level.PROVIDER_DEFAULT).parameters.settings() == {}
    with pytest.raises(ValueError, match=r"disabl|declare"):
        resolve_reasoning(omitted, Level.OFF)
    with pytest.raises(ValidationError, match="cannot be combined"):
        ModelSettingsConfig(id="openai:gpt-6-astra", reasoning_effort=Level.LOW,
                            settings={"openai_reasoning_effort": "high"})
    with pytest.raises(ValidationError, match="declare"):
        ModelSettingsConfig(id="openai:unknown", reasoning_effort=Level.MEDIUM)
    replaced = apply_reasoning({"extra_body": {"reasoning_effort": "high", "other": 7}},
                               resolve_reasoning(omitted, Level.LOW))
    assert replaced["openai_reasoning_effort"] == "low"
    assert replaced["extra_body"] == {"other": 7}
    with pytest.raises(ValueError, match="cannot be combined"):
        ModelSettingsConfig(id="openai:gpt-6-astra", reasoning_effort=Level.LOW,
                            settings={"extra_body": {"reasoning_effort": "high"}})


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("level", [Level.OFF, Level.LOW, Level.MEDIUM, Level.HIGH, Level.MAX])
async def test_explicit_effort_replaces_native_conflicts_on_the_wire(api: str, level: Level) -> None:
    config = ModelSettingsConfig(id="openai:custom-reasoner",
                                reasoning_levels=(Level.OFF, Level.LOW, Level.MEDIUM, Level.HIGH, Level.MAX),
                                settings={"openai_reasoning_effort": "high", "thinking": "high"})
    selection = resolve_reasoning(config, level, source="agent_profile")
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"message": "mock"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(openai_client=AsyncOpenAI(api_key="test", http_client=client))
        model_class = OpenAIResponsesModel if api == "responses" else OpenAIChatModel
        async with PydanticAIModelDriver(model_class("custom-reasoner", provider=provider)) as driver:
            request = replace(_request(), settings=apply_reasoning(config.settings, selection))
            async with driver.open_stream(request) as stream:
                _ = [event async for event in stream.events]
    expected = "none" if level is Level.OFF else level.value
    assert len(bodies) == 1
    assert bodies[0].get("reasoning") == ({"effort": expected} if api == "responses" else None)
    assert bodies[0].get("reasoning_effort") == (expected if api == "chat" else None)
    assert config.settings["openai_reasoning_effort"] == "high"


def test_declared_anthropic_budget_preserves_explicit_cap() -> None:
    budget = ModelSettingsConfig(id="anthropic:custom-model", reasoning_levels=(Level.MEDIUM,))
    assert resolve_reasoning(budget, Level.MEDIUM).parameters.settings() == {
        "anthropic_thinking": {"type": "enabled", "budget_tokens": 10000},
    }
    limited = budget.model_copy(update={"settings": {"max_tokens": 8192}})
    with pytest.raises(ValueError, match="max_tokens"):
        resolve_reasoning(limited, Level.MEDIUM)
    assert limited.settings["max_tokens"] == 8192


@pytest.mark.parametrize("parameters", [
    {"anthropic_thinking": {"type": "adaptive"}, "anthropic_effort": "low"},
    {"anthropic_thinking": {"type": "enabled", "budget_tokens": 2048}, "anthropic_effort": "low"},
    {"google_thinking_config": {"thinking_level": "LOW", "include_thoughts": True}},
])
def test_removed_catalog_models_keep_frozen_parameter_compatibility(parameters: dict[str, Any]) -> None:
    selection = ReasoningSelection.model_validate({
        "requested": "low", "effective": "low", "mapping": "native", "parameters": parameters,
    })
    restored = ReasoningSelection.model_validate_json(selection.model_dump_json())
    assert apply_reasoning({}, restored) == parameters


def test_audit_snapshot_never_contains_unrelated_settings() -> None:
    config = ModelSettingsConfig(id="openai:gpt-6-astra", settings={
        "openai_reasoning_effort": "high", "test_private_field": "not-for-audit",
        "extra_body": {"unrelated_private_field": "not-for-audit"},
    })
    selection = resolve_reasoning(config)
    assert selection.mapping == "unverified"
    assert "not-for-audit" not in selection.model_dump_json()


@pytest.mark.parametrize(("model_id", "base_url", "api"), [
    ("openai:deepseek-flash", "https://api.deepseek.com", "responses"),
    ("anthropic:qwen3.8-max", "https://example.cn-beijing.maas.aliyuncs.com/apps/anthropic", None),
    ("anthropic:qwen3.8-flash", "https://example.cn-beijing.maas.aliyuncs.com/apps/anthropic", None),
    ("openai:qwen3.8-max",
     "https://example.cn-beijing.maas.aliyuncs.com/api/v2/apps/protocols/compatible-mode/v1", "responses"),
    ("openai:qwen3.8-flash",
     "https://example.cn-beijing.maas.aliyuncs.com/api/v2/apps/protocols/compatible-mode/v1", "responses"),
    ("openai:k3", "https://api.kimi.com/coding/v1", "responses"),
    ("anthropic:k3", "https://api.kimi.com/coding", None),
    ("openai:kimi-k3", "https://api.moonshot.cn/v1", "chat"),
])
@pytest.mark.parametrize("level", [Level.OFF, Level.LOW, Level.MEDIUM, Level.HIGH, Level.XHIGH, Level.MAX])
async def test_compatible_provider_controls_reach_real_sdk_http_body(
    model_id: str, base_url: str, api: Literal["chat", "responses"] | None, level: Level,
) -> None:
    config = ModelSettingsConfig(id=model_id, base_url=base_url, api=api)
    is_kimi = ":k3" in model_id or ":kimi-k3" in model_id
    if is_kimi and (level is Level.OFF or ("moonshot" in base_url and level in {Level.MEDIUM, Level.XHIGH})):
        with pytest.raises(ValueError, match="does not declare"):
            resolve_reasoning(config, level)
        return
    if "qwen" in model_id:
        expected = "xhigh" if level in {Level.HIGH, Level.MAX} else level.value
    elif "aliyuncs" in base_url:
        expected = "high" if level in {Level.LOW, Level.MEDIUM} else (
            "max" if level is Level.XHIGH else level.value
        )
    else:
        expected = "high" if level is Level.MEDIUM else ("max" if is_kimi else "high") if (
            level is Level.XHIGH
        ) else level.value
    selection = resolve_reasoning(config, level)
    assert selection.effective == expected
    assert selection.capability_status == "supported"
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"message": "mock"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        name = model_id.partition(":")[2]
        if model_id.startswith("anthropic:"):
            model = AnthropicModel(name, provider=AnthropicProvider(anthropic_client=AsyncAnthropic(
                api_key="test", http_client=client,
            )))
        else:
            model_class = OpenAIResponsesModel if api == "responses" else OpenAIChatModel
            model = model_class(name, provider=OpenAIProvider(openai_client=AsyncOpenAI(
                api_key="test", http_client=client,
            )))
        async with PydanticAIModelDriver(model) as driver:
            request = replace(_request(), settings=selection.parameters.settings())
            async with driver.open_stream(request) as stream:
                _ = [event async for event in stream.events]
    assert len(bodies) == 1
    body = bodies[0]
    if model_id.startswith("anthropic:"):
        assert body["thinking"] == {"type": "disabled" if level is Level.OFF else "enabled"}
        assert body.get("output_config", {}).get("effort") == (None if level is Level.OFF else expected)
    elif api == "responses":
        assert body["reasoning"]["effort"] == ("none" if level is Level.OFF else expected)
    else:
        assert body.get("reasoning_effort") == (None if level is Level.OFF else expected)
        if "deepseek" in model_id:
            assert body["thinking"] == {"type": "disabled" if level is Level.OFF else "enabled"}


def test_capabilities_require_model_and_endpoint_and_keep_defaults_implicit() -> None:
    for config in (
        ModelSettingsConfig(id="anthropic:qwen3.8-max"),
        ModelSettingsConfig(id="openai:deepseek-flash", base_url="https://other.example"),
        ModelSettingsConfig(id="anthropic:unknown", base_url="https://api.anthropic.com"),
    ):
        selection = resolve_reasoning(config)
        assert selection.supported_levels == (Level.PROVIDER_DEFAULT,)
        assert selection.capability_status == "unknown"
    disabled = ModelSettingsConfig(id="openai:custom-model", reasoning_levels=())
    assert resolve_reasoning(disabled).capability_status == "unsupported"
    config = ModelSettingsConfig(id="openai:deepseek-flash", base_url="https://api.deepseek.com")
    assert resolve_reasoning(config).parameters.settings() == {}
    assert resolve_reasoning(config, Level.MINIMAL).effective is Level.LOW
    with pytest.raises(ValueError, match="unsupported"):
        ModelSettingsConfig(id="openai:k3", base_url="https://api.kimi.com/coding/v1",
                            reasoning_levels=(Level.OFF,))
    with pytest.raises(ValueError, match="extra_body must"):
        resolve_reasoning(config.model_copy(update={"settings": {"extra_body": "bad"}}))


def test_explicit_choice_cleans_nested_controls_but_preserves_other_body_fields() -> None:
    config = ModelSettingsConfig(id="anthropic:qwen3.8-max",
                                base_url="https://example.maas.aliyuncs.com/apps/anthropic")
    settings = {"extra_body": {"thinking": {"type": "disabled"}, "reasoning": {"effort": "max"},
                               "output_config": {"effort": "max", "format": {"type": "json_schema"}},
                               "other": "keep"}}
    for level in (Level.LOW, Level.PROVIDER_DEFAULT):
        applied = apply_reasoning(settings, resolve_reasoning(config, level))
        assert applied["extra_body"] == {
            "other": "keep", "output_config": {"format": {"type": "json_schema"}},
        }
    assert settings["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("version", range(1, 10))
def test_reasoning_upgrades_old_session_append_only(tmp_path: Path, version: int) -> None:
    repository = SessionRepository(tmp_path)
    session = repository.create(agent_name="test", model_id="test")
    header = session.path.read_text().replace('"schema_version":11', f'"schema_version":{version}')
    session.path.write_text(header)
    assert not repository.load(session.id).settings.reasoning
    assert session.path.read_text() == header
    selection = resolve_reasoning(ModelSettingsConfig(id="openai:gpt-6-astra"), Level.MEDIUM)
    repository.append_session_settings(session.id, SessionSettingsState(reasoning={"model": selection}))
    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert session.path.read_text().startswith(header)
    assert records[1]["type"] == "schema_upgrade"
    assert records[1]["to_version"] == 10
    assert repository.load(session.id).settings.reasoning["model"] == selection
    repository.append_turn(
        session.id, user_input="done", messages=[], approvals=[], usage={}, status="completed",
    )
    fork = repository.fork(session.id, through_turn=0)
    assert repository.load(fork.id).settings.reasoning["model"] == selection


def manager(tmp_path: Path) -> ResourceManager:
    path = tmp_path / "agent.yaml"
    path.write_text(f"""version: 2
agent:
  models:
    first: {{id: 'openai:gpt-6-astra', api_key: test, reasoning_effort: medium}}
    second: {{id: 'openai:gpt-4o', api_key: test}}
  default_model: first
tools: {{builtins: []}}
sessions: {{directory: '{tmp_path / 'sessions'}'}}
""")
    return ResourceManager(load_config(path), workspace=tmp_path)


@asynccontextmanager
async def opened_host(resources: ResourceManager) -> AsyncGenerator[WorkspaceHost]:
    host = await WorkspaceHost(resources).open()
    try:
        yield host
    finally:
        await host.close()


async def test_host_switch_restore_and_per_model_session_isolation(tmp_path: Path) -> None:
    async with opened_host(manager(tmp_path)) as host:
        session = await host.dispatch(CreateSession())
        other = await host.dispatch(CreateSession())
        await host.dispatch(SelectReasoning(session.session_id, Level.LOW))
        assert (await host.snapshot(session.session_id)).reasoning.effective is Level.LOW
        assert (await host.snapshot(other.session_id)).reasoning.effective is Level.MEDIUM
        await host.dispatch(SelectModel("second"))
        choices = (await host.snapshot(session.session_id)).reasoning.supported_levels
        assert choices == (Level.PROVIDER_DEFAULT,)
        await host.dispatch(SelectModel("first"))
        assert (await host.snapshot(session.session_id)).reasoning.effective is Level.LOW
    async with opened_host(manager(tmp_path)) as restored:
        assert (await restored.snapshot(session.session_id)).reasoning.effective is Level.LOW


async def test_old_session_capabilities_refresh_without_rewriting_history(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    session = resources.session_repository.create(agent_name="test", model_id="openai:gpt-6-astra")
    resources.session_repository.append_session_settings(session.id, SessionSettingsState(reasoning={
        "first:openai:gpt-6-astra": ReasoningSelection(requested=Level.PROVIDER_DEFAULT),
    }))
    previous = session.path.read_bytes()
    async with opened_host(resources) as host:
        snapshot = await host.snapshot(session.id)
        assert snapshot.reasoning.requested is Level.PROVIDER_DEFAULT
        assert Level.LOW in snapshot.reasoning.supported_levels
        assert session.path.read_bytes() == previous
        await host.dispatch(SelectReasoning(session.id, Level.LOW))
    assert session.path.read_bytes().startswith(previous)
    async with opened_host(manager(tmp_path)) as restored:
        assert (await restored.snapshot(session.id)).reasoning.effective is Level.LOW


async def test_startup_choice_overrides_restored_session_once(tmp_path: Path) -> None:
    initial = manager(tmp_path)
    async with opened_host(initial) as host:
        session = await host.dispatch(CreateSession())
        await host.dispatch(SelectReasoning(session.session_id, Level.HIGH))
    resumed = manager(tmp_path)
    resumed.set_startup_reasoning(Level.LOW)
    async with opened_host(resumed) as host:
        assert (await host.dispatch(GetBootstrap())).reasoning.effective is Level.LOW
        assert (await host.snapshot(session.session_id)).reasoning.effective is Level.LOW
        await host.dispatch(SelectReasoning(session.session_id, Level.MEDIUM))
        assert (await host.snapshot(session.session_id)).reasoning.effective is Level.MEDIUM
        await host.dispatch(SelectModel("second"))
        assert (await host.dispatch(GetBootstrap())).reasoning.supported_levels == (Level.PROVIDER_DEFAULT,)
        await host.dispatch(SelectModel("first"))
        assert (await host.snapshot(session.session_id)).reasoning.effective is Level.MEDIUM


def test_startup_choice_overrides_lower_layer_raw_controls(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    resources.active_model_config().settings["extra_body"] = {"reasoning_effort": "high"}
    resources.set_startup_reasoning(Level.LOW)
    assert resources.startup_reasoning is Level.LOW
    assert resources.active_model_config().settings["extra_body"] == {"reasoning_effort": "high"}


async def test_native_child_snapshot_freezes_parent_and_role_override(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    async with resources:
        assert resources.runtime is not None
        cfg = resources.active_model_config()
        resources.runtime.configure_reasoning(resolve_reasoning(cfg, Level.LOW, source="session"))
        profile = resources.agent_profiles["explorer"]
        factory = resources.agent_runtime_factory
        inherited = factory.snapshot(profile, approval_mode="manual")
        assert inherited.reasoning is not None
        assert inherited.reasoning.effective is Level.LOW
        overridden = factory.snapshot(profile.model_copy(update={"reasoning_effort": Level.HIGH}),
                                      approval_mode="manual")
        assert overridden.reasoning is not None and overridden.reasoning.effective is Level.HIGH
        resources.runtime.configure_reasoning(resolve_reasoning(cfg, Level.MEDIUM))
        assert inherited.reasoning.effective is Level.LOW


async def test_default_orchestrator_runs_three_children_with_ordered_spawns(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    resources.agent_orchestrator.config = AgentsConfig()
    release = asyncio.Event()
    ready = asyncio.Event()
    started: list[str] = []
    active = 0
    peak = 0
    async def execute(thread: AgentThreadState, profile: AgentProfile,
                      messages: Sequence[AgentMessage]) -> AgentExecutionResult:
        nonlocal active, peak
        assert isinstance(profile, AgentProfile)
        assert all(isinstance(message, AgentMessage) for message in messages)
        active += 1
        peak = max(peak, active)
        started.append(thread.ref.id)
        if len(started) == 3:
            ready.set()
        try:
            await release.wait()
            return AgentExecutionResult(output="checked")
        finally:
            active -= 1

    resources.agent_runtime_factory.execute = execute
    orchestrator = resources.agent_orchestrator
    session = resources.session_repository.create(agent_name="test", model_id="test")
    orchestrator.bind_root_run(session.id, "run", approval_mode="manual")
    try:
        for index in range(4):
            await orchestrator.spawn_agent(f"task {index}", agent_type="explorer")
        orchestrator.config = AgentsConfig(max_agents_per_run=4)
        replays = await asyncio.gather(*(
            orchestrator.spawn_agent("task 0", agent_type="explorer") for _ in range(3)
        ))
        assert len(set(replays)) == 1
        assert len(orchestrator.list(session.id)) == 4
        await asyncio.wait_for(ready.wait(), 2)
        assert [thread.status.value for thread in orchestrator.list(session.id)] == [
            "running", "running", "running", "queued",
        ]
        release.set()
        async with asyncio.timeout(2):
            while any(thread.result is None for thread in orchestrator.list(session.id)):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert peak == 3
        assert all("agent_queue_seconds" in thread.usage for thread in orchestrator.list(session.id))
    finally:
        await orchestrator.shutdown()


@pytest.mark.parametrize("mode", ["default", "plan"])
@pytest.mark.parametrize("model_name", ["gpt-6-astra", "deepseek-flash"])
async def test_host_freezes_effort_on_wire_and_refuses_changes_during_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["default", "plan"], model_name: str,
) -> None:
    entered = asyncio.Event()
    bodies: list[dict[str, Any]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = OpenAIResponsesModel(model_name, provider=OpenAIProvider(
            openai_client=AsyncOpenAI(api_key="test", http_client=client),
        ))
        def fake_model(_config: ModelSettingsConfig) -> OpenAIResponsesModel:
            return model

        monkeypatch.setattr("lumen.resources.build_model", fake_model)
        resources = manager(tmp_path)
        if model_name == "deepseek-flash":
            resources.active_model_config().id = f"openai:{model_name}"
            resources.active_model_config().base_url = "https://api.deepseek.com"
        async with opened_host(resources) as host:
            session = await host.dispatch(CreateSession())
            await host.dispatch(SetCollaborationMode(session.session_id, mode))
            await host.dispatch(SelectReasoning(session.session_id, Level.LOW))
            started = await host.dispatch(StartRun(session.session_id, "inspect", "test"))
            await asyncio.wait_for(entered.wait(), 3)
            with pytest.raises(WorkspaceBusyError):
                await host.dispatch(SelectReasoning(session.session_id, Level.HIGH))
            await host.dispatch(CancelRun(started.run_id))
            assert bodies[0]["reasoning"] == {"effort": "low"}
            turn = resources.session_repository.load(session.session_id).turns[0]
            prepared = next(item for item in turn.diagnostics if item["kind"] == "model_request_prepared")
            assert prepared["resolved_reasoning"]["effective"] == "low"


async def test_tui_thinking_picker_uses_host_session_state(tmp_path: Path) -> None:
    resources = manager(tmp_path)
    app = LumenApp(resources.config, resources)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await app.handle_input("/thinking low")
        assert app.session is not None
        assert (await app.workspace_host.snapshot(app.session.id)).reasoning.effective is Level.LOW
        await app.handle_input("/thinking")
        await pilot.pause()
        assert isinstance(app.screen, ChoicePickerScreen)
        await pilot.press("escape")
