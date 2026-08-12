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


class EffectKind(StrEnum):
    """Observable consequence of a tool call, independent from security risk.

    ``Risk`` answers whether an invocation needs permission. ``EffectKind``
    answers how the runtime must sequence, journal, and verify its outcome.
    Keeping the two axes separate avoids treating a trusted mutation as a
    read, or an untrusted read as a mutation.
    """

    OBSERVE = "observe"
    MUTATION = "mutation"
    EXECUTION = "execution"
    EXTERNAL_ACTION = "external_action"
    UNKNOWN = "unknown"


def default_effect_for_risk(risk: Risk) -> EffectKind:
    """Compatibility mapping for tools that predate explicit effects."""

    if risk is Risk.READ:
        return EffectKind.OBSERVE
    if risk is Risk.WRITE:
        return EffectKind.MUTATION
    if risk is Risk.EXECUTE:
        return EffectKind.EXECUTION
    if risk is Risk.EXTERNAL:
        return EffectKind.EXTERNAL_ACTION
    return EffectKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class ToolSpec:
    function: Callable[..., Any]
    risk: Risk = Risk.EXECUTE
    name: str | None = None
    description: str | None = None
    timeout: float | None = None
    effect_kind: EffectKind | None = None

    def __post_init__(self) -> None:
        if self.name is None:
            object.__setattr__(self, "name", self.function.__name__)
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError(f"invalid tool name: {self.name!r}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("tool timeout must be positive")

    @property
    def effect(self) -> EffectKind:
        return self.effect_kind or default_effect_for_risk(self.risk)


__all__ = ["EffectKind", "Risk", "ToolSpec", "default_effect_for_risk"]
