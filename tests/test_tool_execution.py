"""Unit tests for the tool execution error-classification seam."""

import pytest
from pydantic_ai.exceptions import ModelRetry

from lumen.tools.execution import (
    ToolErrorKind,
    classify_error,
    is_recoverable,
    to_retry_prompt,
)
from lumen.tools.workspace import WorkspaceViolation


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (FileNotFoundError("file not found: x"), ToolErrorKind.NOT_FOUND),
        (FileExistsError("exists"), ToolErrorKind.EXISTS),
        (NotADirectoryError("nope"), ToolErrorKind.NOT_A_DIRECTORY),
        (IsADirectoryError("is dir"), ToolErrorKind.NOT_A_DIRECTORY),
        (ValueError("0 matches for find"), ToolErrorKind.AMBIGUOUS),
        (ValueError("3 matches for find"), ToolErrorKind.AMBIGUOUS),
        (ValueError("start_line must be positive"), ToolErrorKind.INVALID_ARGUMENT),
        (WorkspaceViolation("escape"), ToolErrorKind.WORKSPACE_VIOLATION),
    ],
)
def test_classify_recoverable_errors(error: Exception, kind: ToolErrorKind) -> None:
    result_kind, message = classify_error(error)
    assert result_kind is kind
    assert message == str(error)


def test_classify_unknown_error_falls_back() -> None:
    error = RuntimeError("boom")
    kind, message = classify_error(error)
    assert kind is ToolErrorKind.UNKNOWN
    assert message == "boom"


def test_is_recoverable_for_known_and_unknown() -> None:
    assert is_recoverable(FileNotFoundError("x")) is True
    assert is_recoverable(ValueError("x")) is True
    assert is_recoverable(WorkspaceViolation("x")) is True
    assert is_recoverable(RuntimeError("x")) is False
    assert is_recoverable(ConnectionError("x")) is False
    assert is_recoverable(TimeoutError("x")) is False


def test_to_retry_prompt_carries_kind_and_message() -> None:
    error = FileNotFoundError("file not found: missing.txt")
    retry = to_retry_prompt(error)
    assert isinstance(retry, ModelRetry)
    assert "[not_found]" in str(retry)
    assert "file not found: missing.txt" in str(retry)


def test_to_retry_prompt_for_ambiguous_edit_is_actionable() -> None:
    error = ValueError("3 matches for find in config.yaml")
    retry = to_retry_prompt(error)
    assert "[ambiguous]" in str(retry)
    assert "3 matches" in str(retry)
