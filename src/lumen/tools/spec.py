from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    EXTERNAL = "external"
    #: Undeclared remote (MCP) tool. Distinct from ``EXTERNAL`` because the
    #: operator never explicitly marked this tool safe — the legacy blanket
    #: ``external`` risk used to auto-approve unknown MCP tools like
    #: ``delete_record`` / ``send_email`` in auto mode. ``external_unknown``
    #: must always confirm regardless of approval mode.
    EXTERNAL_UNKNOWN = "external_unknown"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    function: Callable[..., Any]
    risk: Risk = Risk.EXECUTE
    name: str | None = None
    description: str | None = None
    timeout: float | None = None

    def __post_init__(self) -> None:
        if self.name is None:
            object.__setattr__(self, "name", self.function.__name__)
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"invalid tool name: {self.name!r}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("tool timeout must be positive")
