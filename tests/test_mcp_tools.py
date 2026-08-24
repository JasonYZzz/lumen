from pathlib import Path

import pytest
from pydantic_ai.mcp import MCPToolset

from lumen.config import McpServerConfig, PermissionsConfig
from lumen.mcp_tools import build_mcp_toolset
from lumen.tools.registry import PermissionDecision, PermissionPolicy
from lumen.tools.spec import Risk


def test_build_stdio_mcp_toolset_with_prefix_and_policy() -> None:
    config = McpServerConfig(
        transport="stdio",
        command="python",
        args=["server.py"],
        env={"TOKEN": "secret"},
        read_only_tools=["lookup"],
    )
    policy = PermissionPolicy(PermissionsConfig(always_allow=["demo_mutate"]))

    with pytest.warns(DeprecationWarning, match="read_only_tools"):
        bundle = build_mcp_toolset("demo", config, policy, cwd="/tmp", timeout=15)

    assert isinstance(bundle.client, MCPToolset)
    assert bundle.name == "demo"
    assert bundle.public_name("lookup") == "demo_lookup"
    assert bundle.requires_approval("demo_lookup") is False
    assert bundle.requires_approval("demo_mutate") is False
    assert bundle.requires_approval("demo_other") is True


def test_build_http_mcp_toolset() -> None:
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer token"},
    )

    bundle = build_mcp_toolset("remote", config, PermissionPolicy(PermissionsConfig()), timeout=20)

    assert isinstance(bundle.client, MCPToolset)
    assert bundle.public_name("search") == "remote_search"


def test_undeclared_mcp_tool_defaults_to_external_unknown() -> None:
    """P0: an MCP tool whose risk is not declared must be external_unknown,
    never a silent auto-approve. A ``delete_record`` / ``send_email`` tool
    that the config forgot to classify still requires confirmation."""
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
    )

    bundle = build_mcp_toolset("remote", config, PermissionPolicy(PermissionsConfig()), timeout=20)

    # Undeclared tool → external_unknown (not the legacy blanket "external").
    assert bundle.risk_for("remote_delete_record") is Risk.EXTERNAL_UNKNOWN
    # external_unknown requires approval under the default policy.
    assert bundle.requires_approval("remote_delete_record") is True


def test_tool_risks_override_classifies_declared_tools() -> None:
    """Per-tool risk declarations let users classify MCP tools precisely.
    Declared tools take precedence over the read_only_tools legacy list and
    the external_unknown default."""
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        tool_risks={
            "search": "read",
            "send_email": "write",
            "reboot": "execute",
        },
    )

    bundle = build_mcp_toolset("remote", config, PermissionPolicy(PermissionsConfig()), timeout=20)

    assert bundle.risk_for("remote_search") is Risk.READ
    assert bundle.risk_for("remote_send_email") is Risk.WRITE
    assert bundle.risk_for("remote_reboot") is Risk.EXECUTE
    # Still-undeclared tools remain external_unknown.
    assert bundle.risk_for("remote_other") is Risk.EXTERNAL_UNKNOWN


def test_tool_risks_read_is_auto_approved_in_auto_mode(tmp_path: Path) -> None:
    """In auto mode a tool explicitly declared read is auto-approved, while
    the same tool left undeclared (external_unknown) still confirms."""
    declared = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        tool_risks={"search": "read"},
    )
    undeclared = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
    )
    policy = PermissionPolicy(PermissionsConfig())

    declared_bundle = build_mcp_toolset("remote", declared, policy, timeout=20)
    undeclared_bundle = build_mcp_toolset("remote", undeclared, policy, timeout=20)

    # Declared read → ALLOW under default policy (read auto-approves).
    assert (
        policy.decide("remote_search", declared_bundle.risk_for("remote_search")) is PermissionDecision.ALLOW
    )
    # Undeclared → external_unknown → CONFIRM.
    assert (
        policy.decide("remote_search", undeclared_bundle.risk_for("remote_search"))
        is PermissionDecision.CONFIRM
    )


def test_mcp_tools_are_deferred_by_default_with_explicit_always_load_exceptions() -> None:
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        always_load_tools=["status"],
    )
    bundle = build_mcp_toolset("remote", config, PermissionPolicy(PermissionsConfig()), timeout=20)

    assert bundle.is_deferred("remote_search") is True
    assert bundle.is_deferred("remote_status") is False


def test_mcp_deferred_loading_can_be_disabled_per_server() -> None:
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        defer_tools=False,
    )
    bundle = build_mcp_toolset("remote", config, PermissionPolicy(PermissionsConfig()), timeout=20)

    assert bundle.is_deferred("remote_search") is False


def test_mcp_parallel_safe_only_parallelizes_declared_reads() -> None:
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        tool_risks={"search": "read", "update": "write"},
        tool_effects={"search": "observe", "update": "mutation"},
    )
    bundle = build_mcp_toolset(
        "remote",
        config,
        PermissionPolicy(PermissionsConfig()),
        timeout=20,
        parallel_mode="parallel_safe",
    )

    assert bundle.is_sequential("remote_search", "parallel_safe") is False
    assert bundle.is_sequential("remote_update", "parallel_safe") is True


def test_mcp_effects_are_independent_from_approval_risk() -> None:
    config = McpServerConfig(
        transport="streamable_http",
        url="https://example.test/mcp",
        tool_risks={"lookup": "read"},
        tool_effects={"lookup": "external_action"},
    )
    bundle = build_mcp_toolset(
        "remote",
        config,
        PermissionPolicy(PermissionsConfig()),
        timeout=20,
    )

    assert bundle.risk_for("remote_lookup").value == "read"
    assert bundle.effect_for("remote_lookup").value == "external_action"
    assert bundle.effect_for("remote_undeclared").value == "unknown"
    assert bundle.is_sequential("remote_unknown", "parallel_safe") is True
    assert bundle.is_sequential("remote_update", "parallel") is False
