"""Tests for the run-time command gate."""

from lumen.ui.command_gate import (
    CommandPolicy,
    classify_command,
    classify_model_command,
)


def test_read_only_commands_always_allowed() -> None:
    for line in ["/help", "/mode", "/mode auto", "/tools", "/skills", "/sessions"]:
        assert classify_command(line) is CommandPolicy.ALLOW, line


def test_state_destroyers_are_blocked() -> None:
    for line in ["/clear", "/new", "/resume", "/resume abc-123"]:
        assert classify_command(line) is CommandPolicy.BLOCK, line


def test_retry_and_skill_are_queued() -> None:
    assert classify_command("/retry") is CommandPolicy.QUEUE
    assert classify_command("/skill:tdd") is CommandPolicy.QUEUE
    assert classify_command("/skill:tdd write a test") is CommandPolicy.QUEUE


def test_quit_cancels_then_runs() -> None:
    assert classify_command("/quit") is CommandPolicy.CANCEL_THEN_RUN


def test_model_listing_allowed_but_switching_blocked() -> None:
    assert classify_model_command(["/model"]) is CommandPolicy.ALLOW
    assert classify_model_command(["/model", "gpt-5"]) is CommandPolicy.BLOCK


def test_unknown_command_passes_through() -> None:
    # Unknown commands are allowed so the dispatcher can emit the
    # "unknown command" message rather than a confusing "blocked during run".
    assert classify_command("/frobnicate") is CommandPolicy.ALLOW


def test_base_command_is_lowercased_first_token() -> None:
    # Case-insensitive, and only the first token matters for classification.
    assert classify_command("/HELP") is CommandPolicy.ALLOW
    assert classify_command("  /tools extra args") is CommandPolicy.ALLOW


def test_empty_command_allowed() -> None:
    assert classify_command("") is CommandPolicy.ALLOW
    assert classify_command("   ") is CommandPolicy.ALLOW
