from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
from fastmcp.client.auth import OAuth
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ClientError as FastMCPClientError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset

from lumen.config import McpServerConfig
from lumen.mcp_oauth import JsonCredentialStore
from lumen.tools.registry import PermissionDecision, PermissionPolicy
from lumen.tools.spec import Risk


def _risk_for(config: McpServerConfig, server_name: str, public_name: str) -> Risk:
    """Resolve one public MCP tool name to its configured risk."""

    raw_name = public_name.removeprefix(f"{server_name}_")
    declared = config.tool_risks.get(raw_name)
    if declared is not None:
        return Risk(declared)
    if raw_name in config.read_only_tools:
        return Risk.READ
    return Risk.EXTERNAL_UNKNOWN


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
        return _risk_for(self.config, self.name, public_name)

    def requires_approval(self, public_name: str) -> bool:
        return self.policy.decide(public_name, self.risk_for(public_name)) is PermissionDecision.CONFIRM

    def is_deferred(self, public_name: str) -> bool:
        raw_name = public_name.removeprefix(f"{self.name}_")
        return self.config.defer_tools and raw_name not in self.config.always_load_tools


def build_mcp_toolset(
    name: str,
    config: McpServerConfig,
    policy: PermissionPolicy,
    *,
    cwd: str | Path = ".",
    timeout: float,
    credential_root: str | Path | None = None,
    status_sink: Callable[[str, str], None] | None = None,
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
        auth = None
        if config.oauth is not None:
            credential = Path(config.oauth.credential_file.format(server=name)).expanduser()
            if not credential.is_absolute():
                credential = Path(credential_root or cwd).resolve() / credential
            auth = OAuth(
                mcp_url=config.url,
                scopes=config.oauth.scopes,
                client_name="Lumen",
                token_storage=JsonCredentialStore(credential),  # type: ignore[arg-type]
                callback_port=config.oauth.callback_port,
                callback_timeout=config.oauth.callback_timeout,
                client_id=config.oauth.client_id,
                client_secret=config.oauth.client_secret,
            )
        transport = StreamableHttpTransport(
            config.url,
            headers=config.headers or None,
            auth=auth,  # type: ignore[arg-type]
        )

    client: MCPToolset[None] = MCPToolset(
        transport,
        id=f"mcp:{name}",
        init_timeout=timeout,
        read_timeout=timeout,
    )
    prefixed = client.prefixed(name)

    def is_visible(_ctx: RunContext[None], tool_definition: ToolDefinition) -> bool:
        decision = policy.decide(tool_definition.name, _risk_for(config, name, tool_definition.name))
        return decision is not PermissionDecision.DENY

    visible = prefixed.filtered(is_visible)

    def needs_approval(
        _ctx: RunContext[None], tool_definition: ToolDefinition, _args: dict[str, object]
    ) -> bool:
        return (
            policy.decide(
                tool_definition.name,
                _risk_for(config, name, tool_definition.name),
            )
            is PermissionDecision.CONFIRM
        )

    wrapped: AbstractToolset[None] = visible.approval_required(needs_approval)
    if config.defer_tools:
        always_loaded = frozenset(f"{name}_{raw}" for raw in config.always_load_tools)

        def mark_deferred(
            _ctx: RunContext[None], tool_definitions: list[ToolDefinition]
        ) -> list[ToolDefinition]:
            return [replace(tool, defer_loading=tool.name not in always_loaded) for tool in tool_definitions]

        wrapped = wrapped.prepared(mark_deferred)
    resilient: AbstractToolset[None] = ResilientMcpToolset(
        wrapped, client=client, server_name=name, status_sink=status_sink
    )
    return McpToolsetBundle(name, config, client, resilient, policy)


# --------------------------------------------------------------------------- #
# Transport-level resilience (reconnect + model-visible failure)
# --------------------------------------------------------------------------- #

#: Failures that mean the MCP *connection* is broken (dead stdio process,
#: dropped HTTP stream, torn-down session) rather than the tool call being
#: wrong. Server-side business errors (``ToolError``/``McpError``) are already
#: converted to ``ModelRetry`` by pydantic-ai itself; this set covers what it
#: lets propagate and kill the run.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,  # covers BrokenPipeError / ConnectionResetError
    httpx.HTTPError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    FastMCPClientError,  # fastmcp raises this for calls on a dead session
)


def _is_transport_error(error: BaseException) -> bool:
    """Whether ``error`` indicates a broken MCP transport (reconnectable).

    Exception groups (anyio task-group unwinds) only qualify when *every* leaf
    is a transport error; a group mixing in anything else — notably
    cancellation — is re-raised untouched so it is never swallowed.
    """

    if isinstance(error, _TRANSPORT_ERRORS):
        return True
    if isinstance(error, BaseExceptionGroup):
        # isinstance narrows to BaseExceptionGroup[Unknown]; pin the leaf type
        # by casting the group itself instead of the attribute access.
        group = cast("BaseExceptionGroup[BaseException]", error)
        leaves = group.exceptions
        return len(leaves) > 0 and all(_is_transport_error(leaf) for leaf in leaves)
    return False


class ResilientMcpToolset(WrapperToolset[None]):
    """Outermost MCP wrapper: reconnect once on transport failure, never crash.

    pydantic-ai feeds server-side tool errors back to the model as
    ``ModelRetry``, but a *broken connection* (dead stdio server, dropped HTTP
    stream) propagates and terminates the whole run. This wrapper catches those
    transport errors, forces the underlying ``MCPToolset`` to re-establish its
    session (exit → enter, which also clears its cached tool list), and retries
    the call once. If the server stays unreachable the model receives a
    ``ModelRetry`` explaining the outage instead of the run dying — and every
    later call re-attempts the reconnect, so a restarted server self-heals.
    """

    def __init__(
        self,
        wrapped: AbstractToolset[None],
        *,
        client: MCPToolset[None],
        server_name: str,
        status_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        super().__init__(wrapped)
        self._client = client
        self._server_name = server_name
        self._status_sink = status_sink
        self._reconnect_lock = asyncio.Lock()

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
    ) -> Any:
        try:
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except BaseException as error:
            if not _is_transport_error(error):
                raise
            return await self._reconnect_and_retry(name, tool_args, ctx, tool, error)

    async def _reconnect_and_retry(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[None],
        tool: ToolsetTool[None],
        original_error: BaseException,
    ) -> Any:
        async with self._reconnect_lock:
            try:
                # Drop the broken session. Exiting a half-torn-down connection
                # may itself fail; the enter below rebuilds regardless.
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await self._client.__aenter__()
            except Exception as reconnect_error:
                self._report("error")
                raise ModelRetry(
                    f"[mcp_unavailable] MCP server {self._server_name!r} connection failed "
                    f"({original_error}) and reconnect failed ({reconnect_error}). "
                    "Tell the user the server appears to be down; a later call will retry."
                ) from reconnect_error
        try:
            result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        except BaseException as error:
            if not _is_transport_error(error):
                raise
            self._report("error")
            raise ModelRetry(
                f"[mcp_unavailable] MCP server {self._server_name!r} is still unreachable "
                f"after a reconnect ({error}). Tell the user the server appears to be "
                "down; a later call will retry."
            ) from error
        self._report("ok")
        return result

    def _report(self, status: str) -> None:
        if self._status_sink is not None:
            self._status_sink(self._server_name, status)
