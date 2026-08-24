from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from typing import Any, cast

from pydantic import TypeAdapter

from lumen.tools.presentation import ToolPresentationSpec


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


class ToolConcurrency(StrEnum):
    """Per-invocation concurrency classification, independent from effects."""

    EXCLUSIVE = "exclusive"
    PARALLEL_SAFE = "parallel_safe"


ModelRenderer = Callable[[Any], str]
ClientPresenter = Callable[[Any], dict[str, Any] | None]
ConcurrencyPolicy = Callable[[dict[str, Any]], ToolConcurrency]


def _render_model_default(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True, slots=True)
class ToolOutputSpec:
    """Validate one canonical tool value and derive consumer projections.

    ``value_type`` is intentionally host-only and never appears in the model's
    input schema. The canonical value is converted to JSON mode before either
    renderer sees it, so the Session/UI boundary never receives arbitrary
    Python objects.
    """

    value_type: Any = Any
    render_model: ModelRenderer = _render_model_default
    present: ClientPresenter | None = None
    _adapter: TypeAdapter[Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_adapter", TypeAdapter(self.value_type))

    def validate(self, value: Any) -> Any:
        validated = self._adapter.validate_python(value, strict=True)
        return self._adapter.dump_python(validated, mode="json", warnings="error")

    def model_text(self, value: Any) -> str:
        rendered: Any = self.render_model(value)
        if not isinstance(rendered, str):
            raise TypeError("tool model renderer must return str")
        return rendered

    def presentation(self, value: Any) -> dict[str, Any] | None:
        if self.present is None:
            return None
        rendered = self.present(value)
        if rendered is None:
            return None
        return TypeAdapter(dict[str, Any]).dump_python(
            TypeAdapter(dict[str, Any]).validate_python(rendered, strict=True),
            mode="json",
            warnings="error",
        )


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
    output: ToolOutputSpec | None = None
    concurrency: ConcurrencyPolicy | None = None
    presentation: ToolPresentationSpec | None = None

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

    @property
    def output_contract(self) -> ToolOutputSpec:
        return self.output or ToolOutputSpec()

    def concurrency_for(self, arguments: dict[str, Any]) -> ToolConcurrency:
        if self.concurrency is None:
            return ToolConcurrency.EXCLUSIVE
        return self.concurrency(dict(arguments))

    def model_callable(self) -> Callable[..., Any]:
        """Compatibility Adapter that validates outputs before model exposure."""

        function = self.function
        if self.output is None:
            # Legacy tools preserve their structured return shape so existing
            # runtime diagnostics (for example exit_code extraction) do not
            # lose information. Explicit V2 tools take the validated model
            # projection path below.
            return function
        contract = self.output_contract
        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def async_call(*args: Any, **kwargs: Any) -> str:
                raw = await function(*args, **kwargs)
                canonical = contract.validate(raw)
                return contract.model_text(canonical)

            return cast(Callable[..., Any], async_call)

        @wraps(function)
        def sync_call(*args: Any, **kwargs: Any) -> str:
            canonical = contract.validate(function(*args, **kwargs))
            return contract.model_text(canonical)

        return sync_call


__all__ = [
    "ClientPresenter",
    "ConcurrencyPolicy",
    "EffectKind",
    "ModelRenderer",
    "Risk",
    "ToolConcurrency",
    "ToolOutputSpec",
    "ToolPresentationSpec",
    "ToolSpec",
    "default_effect_for_risk",
]
