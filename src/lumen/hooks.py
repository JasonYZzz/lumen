"""Configurable lifecycle hooks behind one toolset seam."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol, cast

from lumen.config import HookConfig, SandboxConfig
from lumen.sandbox import SandboxRunner
from lumen.tools.capability import run_prepared_command


class HookEvent(StrEnum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    STOP = "stop"
    NOTIFICATION = "notification"


@dataclass(frozen=True, slots=True)
class HookContext:
    event: HookEvent
    session_id: str
    workspace: Path
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["event"] = self.event.value
        value["workspace"] = str(self.workspace)
        return value


@dataclass(frozen=True, slots=True)
class HookDecision:
    allow: bool = True
    modified_args: dict[str, Any] | None = None
    modified_result: str | None = None
    modified_prompt: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class HookDiagnostic:
    event: str
    matcher: str
    message: str
    blocked: bool = False


class HookRunner(Protocol):
    async def run(self, context: HookContext) -> HookDecision: ...


def _decision_from(value: object) -> HookDecision:
    if value is None:
        return HookDecision()
    if isinstance(value, HookDecision):
        return value
    if isinstance(value, Mapping):
        raw = cast(Mapping[str, object], value)
        args = raw.get("modified_args")
        return HookDecision(
            allow=bool(raw.get("allow", True)),
            modified_args=dict(cast(Mapping[str, Any], args)) if isinstance(args, Mapping) else None,
            modified_result=(str(raw["modified_result"]) if raw.get("modified_result") is not None else None),
            modified_prompt=(str(raw["modified_prompt"]) if raw.get("modified_prompt") is not None else None),
            reason=str(raw.get("reason", "")),
        )
    raise TypeError("hook must return HookDecision, mapping, or None")


@dataclass(frozen=True, slots=True)
class CommandHookRunner:
    command: tuple[str, ...]
    timeout: float
    sandbox: SandboxRunner | None = None
    read_paths: tuple[Path, ...] = ()

    async def run(self, context: HookContext) -> HookDecision:
        payload = json.dumps(context.to_dict(), ensure_ascii=False).encode()
        sandbox = self.sandbox or SandboxRunner(context.workspace, SandboxConfig())
        try:
            result = await run_prepared_command(
                sandbox.prepare(
                    self.command_as_list,
                    cwd=context.workspace,
                    read_paths=self.read_paths,
                ),
                argv=self.command_as_list,
                resolved_cwd=context.workspace,
                cwd=str(context.workspace),
                timeout_seconds=self.timeout,
                sandbox_config=sandbox.config,
                stdin_data=payload,
            )
        except Exception as error:
            return HookDecision(
                allow=False,
                reason=f"command hook failed closed: {type(error).__name__}: {error}",
            )
        if result["timed_out"]:
            return HookDecision(allow=False, reason=f"hook timed out after {self.timeout:g}s")
        output = str(result["stdout"]).strip()
        error = str(result["stderr"]).strip()
        if result["exit_code"] == 2:
            return HookDecision(allow=False, reason=error or output or "hook denied operation")
        if result["exit_code"] != 0:
            return HookDecision(
                allow=False,
                reason=error or output or f"hook exited with status {result['exit_code']}",
            )
        if not output:
            return HookDecision()
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            return HookDecision(reason=output)
        return _decision_from(parsed)

    @property
    def command_as_list(self) -> list[str]:
        return list(self.command)


@dataclass(frozen=True, slots=True)
class PythonHookRunner:
    function: Callable[[HookContext], object]

    async def run(self, context: HookContext) -> HookDecision:
        value = self.function(context)
        if inspect.isawaitable(value):
            value = await cast(Any, value)
        return _decision_from(value)


@dataclass(slots=True)
class RegisteredHook:
    event: HookEvent
    matcher: str
    runner: HookRunner
    last_triggered: float | None = None
    deny_count: int = 0


@dataclass
class HookBus:
    workspace: Path
    hooks: list[RegisteredHook] = field(default_factory=list[RegisteredHook])
    diagnostics: list[HookDiagnostic] = field(default_factory=list[HookDiagnostic])
    _session_id: ContextVar[str] = field(
        default_factory=lambda: ContextVar("lumen_hook_session", default="default")
    )

    @classmethod
    def from_config(
        cls,
        configs: list[HookConfig],
        *,
        workspace: Path,
        search_path: Path,
        sandbox_config: SandboxConfig | None = None,
    ) -> HookBus:
        bus = cls(workspace=workspace.resolve())
        for config in configs:
            if config.command is not None:
                runner: HookRunner = CommandHookRunner(
                    tuple(config.command),
                    config.timeout,
                    SandboxRunner(workspace, sandbox_config or SandboxConfig()),
                    _command_read_paths(config.command),
                )
            else:
                assert config.module is not None
                runner = PythonHookRunner(
                    _load_python_hook(config.module, config.factory, search_path=search_path)
                )
            bus.add(RegisteredHook(HookEvent(config.event), config.matcher, runner))
        return bus

    def context(
        self,
        event: HookEvent,
        *,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: str | None = None,
        prompt: str | None = None,
    ) -> HookContext:
        return HookContext(
            event=event,
            session_id=self._session_id.get(),
            workspace=self.workspace,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_result=tool_result,
            prompt=prompt,
        )

    def bind_session(self, session_id: str) -> None:
        """Bind subsequent hook contexts and concurrent child tasks to a run."""

        self._session_id.set(session_id)

    def add(self, hook: RegisteredHook) -> Callable[[], None]:
        """Register one hook and return an identity-safe disposer."""

        self.hooks.append(hook)

        def dispose() -> None:
            for index, current in enumerate(self.hooks):
                if current is hook:
                    del self.hooks[index]
                    break

        return dispose

    async def dispatch(self, context: HookContext) -> HookDecision:
        decision = HookDecision()
        current = context
        for hook in self.hooks:
            if hook.event is not context.event or not self._matches(hook, current.tool_name):
                continue
            hook.last_triggered = time.time()
            try:
                result = await hook.runner.run(current)
            except Exception as error:
                self.diagnostics.append(
                    HookDiagnostic(context.event.value, hook.matcher, str(error), blocked=False)
                )
                continue
            if not result.allow:
                hook.deny_count += 1
                self.diagnostics.append(
                    HookDiagnostic(
                        context.event.value,
                        hook.matcher,
                        result.reason or "hook denied operation",
                        blocked=True,
                    )
                )
                return result
            decision = HookDecision(
                modified_args=result.modified_args or decision.modified_args,
                modified_result=(
                    result.modified_result if result.modified_result is not None else decision.modified_result
                ),
                modified_prompt=(
                    result.modified_prompt if result.modified_prompt is not None else decision.modified_prompt
                ),
                reason=result.reason or decision.reason,
            )
            current = replace(
                current,
                tool_args=decision.modified_args or current.tool_args,
                tool_result=(
                    decision.modified_result if decision.modified_result is not None else current.tool_result
                ),
                prompt=(decision.modified_prompt if decision.modified_prompt is not None else current.prompt),
            )
        return decision

    def summary(self) -> list[dict[str, object]]:
        return [
            {
                "event": hook.event.value,
                "matcher": hook.matcher,
                "runner": type(hook.runner).__name__,
                "last_triggered": hook.last_triggered,
                "deny_count": hook.deny_count,
            }
            for hook in self.hooks
        ]

    @staticmethod
    def _matches(hook: RegisteredHook, tool_name: str | None) -> bool:
        if hook.event in {HookEvent.PRE_TOOL_USE, HookEvent.POST_TOOL_USE}:
            return fnmatchcase(tool_name or "", hook.matcher)
        return True


def _load_python_hook(
    module_name: str,
    factory: str,
    *,
    search_path: Path,
) -> Callable[[HookContext], object]:
    location = str(search_path.resolve())
    sys.path.insert(0, location)
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        raise ImportError(f"cannot import hook module {module_name!r} from {location}: {error}") from error
    finally:
        sys.path.remove(location)
    function = getattr(module, factory, None)
    if not callable(function):
        raise TypeError(f"hook module {module_name!r} has no callable {factory!r}")
    return cast(Callable[[HookContext], object], function)


def _command_read_paths(command: list[str]) -> tuple[Path, ...]:
    """Expose only explicit absolute hook resources outside the workspace."""

    paths: list[Path] = []
    for raw in command:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute() and candidate.exists():
            paths.append(candidate.resolve())
    return tuple(paths)


__all__ = [
    "CommandHookRunner",
    "HookBus",
    "HookContext",
    "HookDecision",
    "HookDiagnostic",
    "HookEvent",
    "PythonHookRunner",
]
