"""Offline fixture loader for the context engine characterization tests.

Fixtures live as static data under ``tests/fixtures/context/`` so they run
without network or provider access. This module turns that raw data into the
in-memory types the context engine consumes (``ModelMessage`` lists, tool
schemas, loaded legacy sessions).

Keeping the bulk content as data files (not hand-serialised ``ModelMessage``
JSON) avoids the fragile pydantic-ai wire format and lets the fixtures be read
and audited by humans.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from lumen.sessions import SessionData, SessionRepository

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "context"


def _read_text(name: str) -> str:
    """Read a UTF-8 fixture file, failing loudly if it is missing."""

    path = FIXTURE_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"missing context fixture: {path}")
    return path.read_text(encoding="utf-8")


def _read_json(name: str) -> Any:
    return json.loads(_read_text(name))


def load_long_shell_history() -> list[ModelMessage]:
    """A history whose single tool return is a large build/shell log.

    Exercises the per-tool-result truncation path and the byte/4 token estimate
    on mixed ASCII output (golden scenario #2: 50 MB-class tool output, scaled
    down to a few KiB so the suite stays fast).
    """

    body = _read_text("long_shell_output.txt")
    return [
        ModelRequest(parts=[UserPromptPart(content="Run the tests and report the result.")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="run_command", args={"command": "uv run pytest -q"}, tool_call_id="shell_1"
                )
            ]
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_command", content=body, tool_call_id="shell_1")]),
        ModelResponse(parts=[TextPart(content="Tests passed; build log captured above.")]),
    ]


def load_mcp_tool_schemas() -> list[dict[str, Any]]:
    """A representative set of MCP tool schemas for budget estimation."""

    schemas = _read_json("mcp_tools_schema.json")
    if not isinstance(schemas, list):
        raise TypeError("mcp_tools_schema.json must be a JSON array of tool schemas")
    return list(schemas)


def load_chinese_history() -> list[ModelMessage]:
    """A multi-turn Chinese conversation mixing JSON, paths and commands.

    Parses the fixture's ``role: text`` lines into messages. ``tool:`` lines
    become a matched call+return pair so tool-pairing invariants hold. Exercises
    the byte/4 estimate on CJK text (golden scenario #5).
    """

    raw = _read_text("chinese_conversation.txt")
    messages: list[ModelMessage] = []
    tool_index = 0
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or ": " not in line:
            continue
        role, _, text = line.partition(": ")
        if role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=text)]))
        elif role == "assistant":
            messages.append(ModelResponse(parts=[TextPart(content=text)]))
        elif role == "tool":
            tool_index += 1
            call_id = f"zh_tool_{tool_index}"
            name, _, summary = text.partition(" ")
            messages.append(
                ModelResponse(
                    parts=[ToolCallPart(tool_name=name or "tool", args={"_": summary}, tool_call_id=call_id)]
                )
            )
            messages.append(
                ModelRequest(
                    parts=[ToolReturnPart(tool_name=name or "tool", content=summary, tool_call_id=call_id)]
                )
            )
    return messages


def load_tool_error_history() -> list[ModelMessage]:
    """A history with a failing tool call (nonzero exit) and an error return.

    Exercises the tool-result status classification and the validate invariant
    on an error-returning turn (golden scenario #6).
    """

    payload = _read_json("tool_error.json")
    call = payload["tool_call"]
    ret = payload["tool_return"]
    return [
        ModelRequest(parts=[UserPromptPart(content=payload["user_prompt"])]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=call["tool_name"], args=call["args"], tool_call_id=call["tool_call_id"]
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name=ret["tool_name"], content=ret["content"], tool_call_id=ret["tool_call_id"]
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="The test failed; the safety invariant needs fixing.")]),
    ]


def load_legacy_session(tmp_path: Path, version: int) -> SessionData:
    """Load a frozen legacy session fixture (schema v1-v4) without rewriting it.

    Copies the fixture into ``tmp_path`` under its canonical id-based filename
    so :class:`SessionRepository` finds it, then loads it. The fixture file
    itself is never mutated, preserving the v1-v4 on-disk format for audit.
    """

    if version not in {1, 2, 3, 4}:
        raise ValueError(f"unsupported legacy session version: {version}")
    fixture_path = FIXTURE_DIR / f"session_v{version}.jsonl"
    if not fixture_path.is_file():
        raise FileNotFoundError(f"missing legacy session fixture: {fixture_path}")
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    header = json.loads(first_line)
    session_id = str(header["id"])
    target = tmp_path / f"{session_id}.jsonl"
    target.write_bytes(fixture_path.read_bytes())
    return SessionRepository(tmp_path).load(session_id)


__all__ = [
    "FIXTURE_DIR",
    "load_chinese_history",
    "load_legacy_session",
    "load_long_shell_history",
    "load_mcp_tool_schemas",
    "load_tool_error_history",
]
