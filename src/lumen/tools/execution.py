"""Tool execution error classification and feedback seam.

The agent loop's most important reliability property: a single recoverable
tool failure (file not found, ambiguous edit, bad plan step) must NOT crash
the whole run. pydantic-ai's default ``on_tool_execute_error`` re-raises the
exception, which propagates out of the stream and terminates the loop — the
model never gets a chance to correct its arguments and retry.

This module is the single seam that decides what happens when a tool raises.
It models the outcome as a :class:`ToolExecutionResult` and converts
recoverable failures into a model-visible retry (``ModelRetry``), so the model
receives a ``RetryPromptPart`` describing the failure and can adjust.

Categorisation follows the plan's spec:

* ``read`` / ``write`` / ``execute`` errors from the builtin + capability
  tools (``FileNotFoundError``, ``NotADirectoryError``, ``FileExistsError``,
  ``ValueError`` argument/match errors, ``WorkspaceViolation``) are
  **recoverable** — the model can fix its arguments.
* Provider faults, cancellations, process-level failures, and unknown
  exceptions are **fatal** — they continue to terminate the run, exactly as
  pydantic-ai's default does, so a genuine crash is never silently swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry

from lumen.tools.workspace import WorkspaceViolation


class ToolErrorKind(StrEnum):
    """The category of a recoverable tool failure, surfaced to the model."""

    NOT_FOUND = "not_found"
    EXISTS = "exists"
    NOT_A_DIRECTORY = "not_a_directory"
    INVALID_ARGUMENT = "invalid_argument"
    AMBIGUOUS = "ambiguous"
    WORKSPACE_VIOLATION = "workspace_violation"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Structured outcome of a tool execution attempt.

    Mirrors the plan's seam so the classification is inspectable and
    testable in isolation from pydantic-ai. ``status`` is ``"success"`` for a
    normal return; ``"error"`` for a recoverable failure that was converted to
    a model retry; ``"denied"`` is reserved for approval denials (handled
    elsewhere). ``retryable`` flags whether the model was given a chance to
    correct its call.
    """

    status: str
    content: str
    error_kind: ToolErrorKind | None = None
    error_type: str | None = None
    retryable: bool = False


# Exceptions that represent a recoverable misuse the model can fix by
# adjusting its arguments. Anything outside this set is fatal and propagates.
_RECOVERABLE: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    NotADirectoryError,
    FileExistsError,
    IsADirectoryError,
    ValueError,
    WorkspaceViolation,
)


def classify_error(error: BaseException) -> tuple[ToolErrorKind, str]:
    """Map an exception to a (kind, message) the model can act on.

    Returns a recoverable kind for known argument/path mistakes. For an
    unrecognised exception this raises ``error`` re-raised (the caller —
    pydantic-ai's error hook — treats an unhandled re-raise as fatal).
    """

    message = str(error) or error.__class__.__name__
    if isinstance(error, WorkspaceViolation):
        return ToolErrorKind.WORKSPACE_VIOLATION, message
    if isinstance(error, FileNotFoundError):
        return ToolErrorKind.NOT_FOUND, message
    if isinstance(error, FileExistsError):
        return ToolErrorKind.EXISTS, message
    if isinstance(error, NotADirectoryError | IsADirectoryError):
        return ToolErrorKind.NOT_A_DIRECTORY, message
    if isinstance(error, ValueError):
        lowered = message.lower()
        if "match" in lowered:
            return ToolErrorKind.AMBIGUOUS, message
        return ToolErrorKind.INVALID_ARGUMENT, message
    return ToolErrorKind.UNKNOWN, message


def is_recoverable(error: BaseException) -> bool:
    """Whether ``error`` should be fed back to the model rather than crash."""
    return isinstance(error, _RECOVERABLE)


def to_retry_prompt(error: BaseException) -> ModelRetry:
    """Convert a recoverable tool error into a model-visible retry prompt.

    The message names the failure and the kind, so the model gets an
    actionable hint (e.g. ``[not_found] file not found: missing.txt``) and
    can choose a corrective action.
    """

    kind, message = classify_error(error)
    return ModelRetry(f"[{kind.value}] {message}")


class RecoverableToolErrors(AbstractCapability[None]):
    """Capability that feeds recoverable tool exceptions back to the model.

    Attached per-run alongside :class:`HandleDeferredToolCalls`. Overrides
    ``on_tool_execute_error`` so a recoverable exception becomes a
    :class:`ModelRetry` (pydantic-ai surfaces it to the model as a
    ``RetryPromptPart`` and the loop continues), while fatal exceptions
    re-raise unchanged.
    """

    async def on_tool_execute_error(  # type: ignore[override]
        self,
        ctx: object,
        *,
        call: object,
        tool_def: object,
        args: object,
        error: Exception,
    ) -> object:
        if is_recoverable(error):
            raise to_retry_prompt(error)
        raise error


__all__ = [
    "RecoverableToolErrors",
    "ToolErrorKind",
    "ToolExecutionResult",
    "classify_error",
    "is_recoverable",
    "to_retry_prompt",
]
