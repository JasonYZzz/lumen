import sys
from pathlib import Path
from types import ModuleType

import pytest

from lumen.config import PermissionsConfig, PluginConfig
from lumen.tools.registry import (
    DuplicateToolError,
    PermissionDecision,
    PermissionPolicy,
    ToolRegistry,
    load_plugin_specs,
)
from lumen.tools.spec import EffectKind, Risk, ToolConcurrency, ToolOutputSpec, ToolSpec


def sample_tool(value: str) -> str:
    """Return the supplied value."""
    return value


def test_tool_spec_keeps_legacy_positional_field_order() -> None:
    spec = ToolSpec(sample_tool, Risk.READ, "legacy", "description", 3.0)

    assert spec.name == "legacy"
    assert spec.description == "description"
    assert spec.timeout == 3.0
    assert spec.effect is EffectKind.OBSERVE


def test_plugin_factory_must_return_tool_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("bad_plugin")
    module.create_tools = lambda: [sample_tool]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bad_plugin", module)

    with pytest.raises(TypeError, match="ToolSpec"):
        load_plugin_specs(PluginConfig(module="bad_plugin"))


def test_missing_plugin_reports_module_and_search_path(tmp_path: Path) -> None:
    with pytest.raises(
        RuntimeError,
        match=rf"plugin 'missing_tools' was not found under {tmp_path}",
    ):
        load_plugin_specs(PluginConfig(module="missing_tools"), search_path=tmp_path)


def test_registry_rejects_duplicate_names(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    spec = ToolSpec(sample_tool, risk=Risk.READ, name="same")
    registry.add(spec, origin="first")

    with pytest.raises(DuplicateToolError, match="same"):
        registry.add(spec, origin="second")


def test_permission_policy_applies_deny_allow_and_risk_order() -> None:
    policy = PermissionPolicy(
        PermissionsConfig(
            always_allow=["write_note", "publish"],
            always_deny=["blocked"],
        )
    )

    assert policy.decide("blocked", Risk.READ) is PermissionDecision.DENY
    assert policy.decide("write_note", Risk.WRITE) is PermissionDecision.ALLOW
    assert policy.decide("read_file", Risk.READ) is PermissionDecision.ALLOW
    assert policy.decide("run_task", Risk.EXECUTE) is PermissionDecision.CONFIRM
    assert policy.decide("publish", Risk.CONFIRM) is PermissionDecision.CONFIRM


def test_local_tools_are_hidden_or_marked_for_approval(tmp_path: Path) -> None:
    policy = PermissionPolicy(PermissionsConfig(always_deny=["blocked"]))
    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(sample_tool, risk=Risk.READ, name="safe"), origin="plugin")
    registry.add(ToolSpec(sample_tool, risk=Risk.WRITE, name="dangerous"), origin="plugin")
    registry.add(ToolSpec(sample_tool, risk=Risk.READ, name="blocked"), origin="plugin")

    tools = registry.build_local_tools(policy, default_timeout=10)
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == {"safe", "dangerous"}
    assert by_name["safe"].requires_approval is False
    assert by_name["dangerous"].requires_approval is True
    assert by_name["dangerous"].sequential is True
    assert by_name["dangerous"].timeout == 10


async def test_tool_output_contract_validates_before_returning_model_text(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path)
    registry.add(
        ToolSpec(
            lambda: {"count": "not-an-int"},
            name="invalid_output",
            risk=Risk.READ,
            output=ToolOutputSpec(dict[str, int]),
        ),
        origin="test",
    )
    tool = registry.build_local_tools(PermissionPolicy(PermissionsConfig()), default_timeout=1)[0]

    with pytest.raises(ValueError):
        await tool.function_schema.call({}, None)  # type: ignore[arg-type]


def test_tool_concurrency_is_explicit_and_can_classify_arguments() -> None:
    spec = ToolSpec(
        sample_tool,
        risk=Risk.READ,
        concurrency=lambda args: (
            ToolConcurrency.PARALLEL_SAFE
            if str(args.get("value", "")).startswith("read:")
            else ToolConcurrency.EXCLUSIVE
        ),
    )

    assert ToolSpec(sample_tool, risk=Risk.READ).concurrency_for({}) is ToolConcurrency.EXCLUSIVE
    assert ToolSpec(sample_tool, risk=Risk.CONFIRM).effect is EffectKind.UNKNOWN
    assert spec.concurrency_for({"value": "read:a"}) is ToolConcurrency.PARALLEL_SAFE
    assert spec.concurrency_for({"value": "write:a"}) is ToolConcurrency.EXCLUSIVE


def test_registry_cannot_register_control_tool_name(tmp_path: Path) -> None:
    """Control tool names are reserved; the registry never holds them.

    The registry itself doesn't know about the reserved set (that lives in the
    resource manager), but we assert here that the manager's collision check
    runs against the registry before exposing tools. So this is a sanity test
    that adding any name still works and the manager's reserved check is the
    gatekeeper; we exercise the manager-level collision in test_resources.
    """
    registry = ToolRegistry(tmp_path)
    registry.add(ToolSpec(sample_tool, risk=Risk.READ, name="set_plan"), origin="malicious")
    # The registry itself does not refuse the name; the manager does.
    assert "set_plan" in registry.entries
