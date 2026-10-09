from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from lumen.sessions import SessionCorruptError, SessionRepository, TurnCheckpointBoundary, turn_message_digest


def batch(call_id: str) -> list[ModelMessage]:
    return [ModelResponse(parts=[ToolCallPart("read_file", {"path": "a.txt"}, call_id)]),
            ModelRequest(parts=[ToolReturnPart("read_file", "synthetic", call_id)])]


@pytest.mark.parametrize("version", range(1, 12))
def test_batch_checkpoint_recovery_and_terminal_supersession_are_append_only(
    tmp_path: Path, version: int,
) -> None:
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="offline")
    original = session.path.read_text().replace('"schema_version":12', f'"schema_version":{version}')
    session.path.write_text(original)
    repo.append_turn_started(session.id, user_input="inspect", interaction_id="interaction")
    messages: list[ModelMessage] = []
    for sequence in (1, 2):
        delta = (
            [ModelRequest(parts=[UserPromptPart("inspect")]), *batch("one")]
            if sequence == 1 else batch("two")
        )
        combined = [*messages, *delta]
        marker = TurnCheckpointBoundary(
            sequence=sequence, prefix_count=len(messages), prefix_digest=turn_message_digest(messages),
            message_count=len(combined), message_digest=turn_message_digest(combined),
        )
        repo.append_turn(session.id, user_input="inspect", messages=delta, approvals=[], usage={},
                         status="running", interaction_id="interaction", checkpoint=marker,
                         diagnostics=[{"kind": "completed_model_steps", "message_count": len(combined)}])
        messages = combined
        cold = SessionRepository(tmp_path).load(session.id)
        assert len(cold.turns) == 1 and cold.turns[0].status == "running"
        assert cold.history == cold.full_history == messages
        assert cold == repo.load(session.id)
    raw = json.loads(session.path.read_text().splitlines()[-1])
    with session.path.open("a") as handle:
        handle.write(json.dumps(raw) + "\n")
    assert SessionRepository(tmp_path).load(session.id).history == messages
    terminal = [*messages, ModelResponse(parts=[TextPart("done")])]
    repo.append_turn(session.id, user_input="inspect", messages=terminal, approvals=[], usage={},
                     status="completed", interaction_id="interaction")
    loaded = SessionRepository(tmp_path).load(session.id)
    assert len(loaded.turns) == 1 and loaded.history == terminal
    assert session.path.read_text().startswith(original)


def test_corrupt_checkpoint_boundary_is_rejected_without_repair(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="offline")
    repo.append_turn_started(session.id, user_input="inspect", interaction_id="interaction")
    messages = [ModelRequest(parts=[UserPromptPart("inspect")]), *batch("one")]
    repo.append_turn(session.id, user_input="inspect", messages=messages, approvals=[], usage={},
                     status="running", interaction_id="interaction", checkpoint=TurnCheckpointBoundary(
                         sequence=1, prefix_count=0, prefix_digest=turn_message_digest([]),
                         message_count=len(messages), message_digest="sha256:" + "0" * 64,
                     ))
    with pytest.raises(SessionCorruptError, match="boundary mismatch"):
        repo.load(session.id)
    with pytest.raises(SessionCorruptError, match="incomplete final line"):
        repo.inspect_tail_repair(session.id)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_large_checkpoint_is_content_addressed_and_missing_blob_fails_closed(
    tmp_path: Path, damage: str,
) -> None:
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="offline")
    repo.append_turn_started(session.id, user_input="inspect", interaction_id="interaction")
    messages: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("inspect")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "a.txt"}, "one")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "synthetic " * 10000, "one")])]
    repo.append_turn(session.id, user_input="inspect", messages=messages, approvals=[], usage={},
        status="running", interaction_id="interaction", checkpoint=TurnCheckpointBoundary(
            sequence=1, prefix_count=0, prefix_digest=turn_message_digest([]), message_count=3,
            message_digest=turn_message_digest(messages)),
        diagnostics=[{"kind": "completed_model_steps", "message_count": 3}])
    record = json.loads(session.path.read_text().splitlines()[-1])
    assert record["payload_bytes"] > 65536 and session.path.stat().st_size < 4096
    assert SessionRepository(tmp_path).load(session.id).history == messages
    blob = tmp_path / "checkpoint-artifacts" / record["payload_ref"].split(":")[1]
    if damage == "missing":
        blob.unlink()
    else:
        blob.write_bytes(b"corrupt")
    with pytest.raises(SessionCorruptError, match="turn_checkpoint"):
        repo.load(session.id, revalidate=True)


def test_explicit_tail_repair_preserves_original_and_rejects_stale_preview(tmp_path: Path) -> None:
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="offline")
    prefix = session.path.read_bytes()
    original = prefix + b'{"type":"turn","user_input":"partial'
    session.path.write_bytes(original)
    with pytest.raises(SessionCorruptError):
        repo.load(session.id)
    preview = repo.inspect_tail_repair(session.id)
    with pytest.raises(SessionCorruptError, match="stale"):
        repo.repair_tail(session.id, expected_digest="sha256:" + "0" * 64)
    result = repo.repair_tail(session.id, expected_digest=preview["original_digest"])
    assert (tmp_path / result["backup"]).read_bytes() == original
    assert session.path.read_bytes().startswith(prefix)
    assert repo.load(session.id).turns == []
    records = [json.loads(line) for line in session.path.read_text().splitlines()]
    assert records[-1]["type"] == "session_repair"


@pytest.mark.parametrize("tail", [b'{"type":"unknown"}\n', b'garbage', b'{"schema_version":999}'])
def test_repair_does_not_drop_complete_or_arbitrarily_corrupt_records(tmp_path: Path, tail: bytes) -> None:
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="offline")
    original = session.path.read_bytes() + tail
    session.path.write_bytes(original)
    with pytest.raises(SessionCorruptError):
        repo.inspect_tail_repair(session.id)
    assert session.path.read_bytes() == original


@pytest.mark.parametrize("failure", ["backup", "replace", "audit"])
def test_repair_failures_preserve_original_and_never_report_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import os
    from typing import Any, cast
    repo = SessionRepository(tmp_path)
    session = repo.create(agent_name="test", model_id="offline")
    original = session.path.read_bytes() + b'{"type":"turn'
    session.path.write_bytes(original)
    preview = repo.inspect_tail_repair(session.id)
    original_open, original_replace = Path.open, os.replace
    original_append = repo._append  # pyright: ignore[reportPrivateUsage]
    def open_file(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name.endswith(".corrupt-backup"):
            raise OSError("synthetic backup failure")
        return cast(Any, original_open(path, *args, **kwargs))
    def replace_file(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("synthetic replace failure")
    def append_record(path: Path, record: dict[str, Any], *, create: bool = False) -> None:
        if record["type"] == "session_repair":
            raise OSError("synthetic audit failure")
        original_append(path, record, create=create)
    if failure == "backup":
        monkeypatch.setattr(Path, "open", open_file)
    elif failure == "replace":
        monkeypatch.setattr(os, "replace", replace_file)
    else:
        monkeypatch.setattr(repo, "_append", append_record)
    with pytest.raises(OSError, match="synthetic"):
        repo.repair_tail(session.id, expected_digest=preview["original_digest"])
    monkeypatch.setattr(Path, "open", original_open)
    monkeypatch.setattr(os, "replace", original_replace)
    if failure == "backup":
        assert session.path.read_bytes() == original
    else:
        backups = list(tmp_path.glob("*.corrupt-backup"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        if failure == "replace":
            assert session.path.read_bytes() == original
        else:
            assert repo.load(session.id, revalidate=True).turns == []
