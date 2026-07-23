from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset

from lumen.config import McpServerConfig
from lumen.tools.registry import PermissionDecision, PermissionPolicy
from lumen.tools.spec import Risk


@dataclass(frozen=True, slots=True)
class McpToolsetBundle:
    name: str
    config: McpServerConfig
    client: MCPToolset[None]
    toolset: AbstractToolset[None]
    policy: PermissionPolicy

    def public_name(self, tool_name: str) -> str:
        prefix = f"{self.name}_"
        return tool_name if tool_name.startswith(prefix) else f"{prefix}{tool_name}"

    def risk_for(self, public_name: str) -> Risk:
        """Classify an MCP tool's risk.

        Resolution order: ``tool_risks`` declaration → legacy
        ``read_only_tools`` (maps to read, with a deprecation warning at build
        time) → ``external_unknown``. The ``external_unknown`` default means a
        tool the operator never classified (e.g. ``delete_record``,
        ``send_email``) is never silently auto-approved.
        """
        prefix = f"{self.name}_"
        raw_name = public_name.removeprefix(prefix)
        declared = self.config.tool_risks.get(raw_name)
        if declared is not None:
            return Risk(declared)
        if raw_name in self.config.read_only_tools:
            return Risk.READ
        return Risk.EXTERNAL_UNKNOWN

    def requires_approval(self, public_name: str) -> bool:
        return self.policy.decide(public_name, self.risk_for(public_name)) is PermissionDecision.CONFIRM


def build_mcp_toolset(
    name: str,
    config: McpServerConfig,
    policy: PermissionPolicy,
    *,
    cwd: str | Path = ".",
    timeout: float,
) -> McpToolsetBundle:
    if not name.replace("-", "_").replace("_", "").isalnum():
        raise ValueError(f"invalid MCP server name: {name!r}")
    if config.read_only_tools:
        warnings.warn(
            f"MCP server {name!r}: 'read_only_tools' is deprecated; declare each "
            "tool's risk under 'tool_risks' instead (e.g. tool_risks: "
            "{lookup: read}). 'read_only_tools' still works but classifies "
            "tools only as read.",
            DeprecationWarning,
            stacklevel=2,
        )
    if config.transport == "stdio":
        assert config.command is not None
        transport = StdioTransport(
            config.command,
            config.args,
            env=config.env or None,
            cwd=str(Path(cwd).resolve()),
        )
    else:
        assert config.url is not None
        transport = StreamableHttpTransport(config.url, headers=config.headers or None)

    client: MCPToolset[None] = MCPToolset(
        transport,
        id=f"mcp:{name}",
        init_timeout=timeout,
        read_timeout=timeout,
    )
    prefix = f"{name}_"
    prefixed = client.prefixed(name)

    # Single source of truth for tool → risk, mirroring
    # :meth:`McpToolsetBundle.risk_for` so visibility and approval agree.
    def _resolve_risk(public_name: str) -> Risk:
        raw_name = public_name.removeprefix(prefix)
        declared = config.tool_risks.get(raw_name)
        if declared is not None:
            return Risk(declared)
        if raw_name in config.read_only_tools:
            return Risk.READ
        return Risk.EXTERNAL_UNKNOWN

    def is_visible(_ctx: RunContext[None], tool_definition: ToolDefinition) -> bool:
        decision = policy.decide(tool_definition.name, _resolve_risk(tool_definition.name))
        return decision is not PermissionDecision.DENY

    visible = prefixed.filtered(is_visible)

    def needs_approval(
        _ctx: RunContext[None], tool_definition: ToolDefinition, _args: dict[str, object]
    ) -> bool:
        return (
            policy.decide(tool_definition.name, _resolve_risk(tool_definition.name))
            is PermissionDecision.CONFIRM
        )

    wrapped = visible.approval_required(needs_approval)
    return McpToolsetBundle(name, config, client, wrapped, policy)
