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
