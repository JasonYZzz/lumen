from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from pydantic_ai import Tool

from lumen.config import PermissionsConfig, PluginConfig
from lumen.tools.spec import Risk, ToolSpec


class DuplicateToolError(ValueError):
    """Raised when two configured tools expose the same model-visible name."""


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class PermissionPolicy:
    def __init__(self, config: PermissionsConfig) -> None:
        self.always_allow = frozenset(config.always_allow)
        self.always_deny = frozenset(config.always_deny)

    def decide(self, name: str, risk: Risk) -> PermissionDecision:
        if name in self.always_deny:
            return PermissionDecision.DENY
        if name in self.always_allow or risk is Risk.READ:
            return PermissionDecision.ALLOW
        return PermissionDecision.CONFIRM


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    spec: ToolSpec
    origin: str


def load_plugin_specs(config: PluginConfig, *, search_path: str | Path | None = None) -> list[ToolSpec]:
    added_path = str(Path(search_path).resolve()) if search_path is not None else None
    if added_path is not None:
        sys.path.insert(0, added_path)
    try:
        module = importlib.import_module(config.module)
    finally:
        if added_path is not None:
            sys.path.remove(added_path)
    factory = getattr(module, config.factory, None)
    if not callable(factory):
        raise TypeError(f"plugin {config.module} has no callable {config.factory}")
    result: Any = factory()
    if not isinstance(result, list):
        raise TypeError(f"plugin {config.module}.{config.factory} must return list[ToolSpec]")
    items = cast(list[object], result)
    if any(not isinstance(item, ToolSpec) for item in items):
        raise TypeError(f"plugin {config.module}.{config.factory} must return list[ToolSpec]")
    return cast(list[ToolSpec], items)


class ToolRegistry:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._entries: dict[str, RegisteredTool] = {}

    @property
    def entries(self) -> dict[str, RegisteredTool]:
        return dict(self._entries)

    def add(self, spec: ToolSpec, *, origin: str) -> None:
        name = spec.name
        if name is None:  # ToolSpec fills this, but keep the registry defensive.
            raise ValueError("tool has no name")
        if name in self._entries:
            previous = self._entries[name]
            raise DuplicateToolError(f"tool {name!r} from {origin} conflicts with {previous.origin}")
        self._entries[name] = RegisteredTool(spec, origin)

    def add_many(self, specs: list[ToolSpec], *, origin: str) -> None:
        for spec in specs:
            self.add(spec, origin=origin)

    def build_local_tools(self, policy: PermissionPolicy, *, default_timeout: float) -> list[Tool[None]]:
        tools: list[Tool[None]] = []
        for name, entry in self._entries.items():
            decision = policy.decide(name, entry.spec.risk)
            if decision is PermissionDecision.DENY:
                continue
            tools.append(
                Tool(
                    entry.spec.function,
                    name=name,
                    description=entry.spec.description,
                    sequential=True,
                    requires_approval=decision is PermissionDecision.CONFIRM,
                    timeout=entry.spec.timeout or default_timeout,
                    metadata={"origin": entry.origin, "risk": entry.spec.risk.value},
                )
            )
        return tools
