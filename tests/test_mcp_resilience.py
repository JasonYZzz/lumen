"""Effect-aware recovery contracts for the MCP transport Adapter.

A broken MCP *connection* (dead stdio process, dropped HTTP stream) must not
terminate the agent run. Declared observe tools may reconnect and retry once;
unknown or mutating effects return ``mcp_outcome_unknown`` without replay.
These tests pin the transport policy, cancellation, and guarded teardown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool

from lumen.config import McpServerConfig, PermissionsConfig
from lumen.mcp_tools import (
    ResilientMcpToolset,
    _is_transport_error,  # type: ignore[reportPrivateUsage]
    build_mcp_toolset,
)
from lumen.resources import _guarded_mcp_client_exit  # type: ignore[reportPrivateUsage]
from lumen.tools.registry import PermissionPolicy

_CTX = cast(RunContext[None], None)
_TOOL = cast(ToolsetTool[None], None)


class _FakeInnerToolset:
    """Stands in for the wrapped chain; raises queued failures then echoes."""

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0

    async def call_tool(self, name: str, tool_args: dict[str, Any], *_args: object) -> object:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"echo": tool_args}


class _FakeClient:
    """Records enter/exit; can be rigged to fail the reconnect enter."""

    def __init__(self, *, fail_enter: bool = False) -> None:
        self.enters = 0
        self.exits = 0
        self.fail_enter = fail_enter

    async def __aenter__(self) -> _FakeClient:
        self.enters += 1
        if self.fail_enter:
            raise ConnectionError("server down")
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.exits += 1


def _make_toolset(
    inner: _FakeInnerToolset,
    client: _FakeClient,
    statuses: list[tuple[str, str]] | None = None,
) -> ResilientMcpToolset:
    sink: Callable[[str, str], None] | None = (
        (lambda name, status: statuses.append((name, status))) if statuses is not None else None
    )
    return ResilientMcpToolset(
        cast(AbstractToolset[None], inner),
        client=cast(MCPToolset[None], client),
        server_name="calc",
        status_sink=sink,
        retryable_tools=frozenset({"calc_add"}),
    )


# -- transport error classification ------------------------------------------


def test_transport_error_classification() -> None:
    assert _is_transport_error(ConnectionError("reset"))
    assert _is_transport_error(httpx.ConnectError("refused"))
    assert _is_transport_error(ExceptionGroup("eg", [ConnectionError("a"), httpx.ReadError("b")]))
    assert not _is_transport_error(ValueError("bad args"))
    assert not _is_transport_error(asyncio.CancelledError())
    # A group mixing in a foreign leaf is not transport-only.
    assert not _is_transport_error(ExceptionGroup("eg", [ConnectionError("a"), ValueError("b")]))


# -- reconnect + retry --------------------------------------------------------


async def test_undeclared_effect_is_not_replayed_after_ambiguous_disconnect() -> None:
    inner = _FakeInnerToolset([ConnectionError("response lost after executing")])
    client = _FakeClient()
    toolset = ResilientMcpToolset(
        cast(AbstractToolset[None], inner),
        client=cast(MCPToolset[None], client),
        server_name="remote",
    )
    with pytest.raises(ModelRetry, match="mcp_outcome_unknown"):
        await toolset.call_tool("remote_action", {}, _CTX, _TOOL)
    assert inner.calls == 1
    assert client.exits == client.enters == 0


@pytest.mark.parametrize("observe", [False, True])
async def test_retry_policy_uses_effect_declaration_not_read_risk(
    observe: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = build_mcp_toolset(
        "calc",
        McpServerConfig(
            transport="streamable_http", url="https://example.test/mcp",
            tool_risks={"add": "read"}, tool_effects={"add": "observe"} if observe else {},
        ),
        PermissionPolicy(PermissionsConfig()),
        timeout=5,
    )
    assert isinstance(bundle.toolset, ResilientMcpToolset)
    inner = _FakeInnerToolset([ConnectionError("lost response")])
    client = _FakeClient()
    monkeypatch.setattr(bundle.toolset, "wrapped", inner)
    monkeypatch.setattr(bundle.toolset, "_client", client)
    if observe:
        assert await bundle.toolset.call_tool("calc_add", {}, _CTX, _TOOL) == {"echo": {}}
    else:
        with pytest.raises(ModelRetry, match="mcp_outcome_unknown"):
            await bundle.toolset.call_tool("calc_add", {}, _CTX, _TOOL)
    assert inner.calls == (2 if observe else 1)


async def test_transport_error_reconnects_and_retries() -> None:
    inner = _FakeInnerToolset([ConnectionError("broken pipe")])
    client = _FakeClient()
    statuses: list[tuple[str, str]] = []
    toolset = _make_toolset(inner, client, statuses)

    result = await toolset.call_tool("calc_add", {"a": 1}, _CTX, _TOOL)

    assert result == {"echo": {"a": 1}}
    assert inner.calls == 2  # initial failure + one retry after reconnect
    assert client.exits == 1 and client.enters == 1
    assert statuses == [("calc", "ok")]


async def test_reconnect_failure_becomes_model_retry_and_reheals_later() -> None:
    inner = _FakeInnerToolset([ConnectionError("x"), ConnectionError("y")])
    client = _FakeClient(fail_enter=True)
    statuses: list[tuple[str, str]] = []
    toolset = _make_toolset(inner, client, statuses)

    with pytest.raises(ModelRetry, match=r"mcp_unavailable.*'calc'"):
        await toolset.call_tool("calc_add", {}, _CTX, _TOOL)
    assert statuses == [("calc", "error")]

    # A later call re-attempts the reconnect rather than staying fatal.
    with pytest.raises(ModelRetry):
        await toolset.call_tool("calc_add", {}, _CTX, _TOOL)
    assert client.enters == 2


async def test_retry_still_failing_becomes_model_retry() -> None:
    inner = _FakeInnerToolset([ConnectionError("x"), httpx.ConnectError("y")])
    client = _FakeClient()
    statuses: list[tuple[str, str]] = []
    toolset = _make_toolset(inner, client, statuses)

    with pytest.raises(ModelRetry, match=r"still unreachable"):
        await toolset.call_tool("calc_add", {}, _CTX, _TOOL)
    assert inner.calls == 2
    assert statuses == [("calc", "error")]


async def test_non_transport_error_propagates_without_reconnect() -> None:
    inner = _FakeInnerToolset([ValueError("bad arguments")])
    client = _FakeClient()
    toolset = _make_toolset(inner, client)

    with pytest.raises(ValueError, match="bad arguments"):
        await toolset.call_tool("calc_add", {}, _CTX, _TOOL)
    assert client.exits == 0 and client.enters == 0


async def test_cancellation_is_never_swallowed() -> None:
    inner = _FakeInnerToolset([asyncio.CancelledError()])
    client = _FakeClient()
    toolset = _make_toolset(inner, client)

    with pytest.raises(asyncio.CancelledError):
        await toolset.call_tool("calc_add", {}, _CTX, _TOOL)
    assert client.exits == 0


async def test_exception_group_of_transport_errors_reconnects() -> None:
    group = ExceptionGroup("eg", [ConnectionError("a"), httpx.ConnectError("b")])
    inner = _FakeInnerToolset([group])
    client = _FakeClient()
    toolset = _make_toolset(inner, client)

    result = await toolset.call_tool("calc_add", {}, _CTX, _TOOL)

    assert result == {"echo": {}}
    assert client.exits == 1 and client.enters == 1


# -- guarded session teardown -------------------------------------------------


async def test_guarded_exit_tolerates_reconnect_drained_client() -> None:
    class _DrainedClient:
        async def __aexit__(self, *_args: object) -> None:
            raise ValueError("called more times than enters")

    # Must not raise: a server that stayed dead left the count at zero.
    await _guarded_mcp_client_exit(cast(MCPToolset[None], _DrainedClient()))
