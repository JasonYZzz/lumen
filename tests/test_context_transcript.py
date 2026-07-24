"""Tests for tool-output reduction and receipts (M3)."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from lumen.context.artifacts import ArtifactStore
from lumen.context.legacy import validate_active_history
from lumen.context.transcript import build_receipt, reduce_tool_outputs


def _store(tmp_path: Path, *, inline: int = 64) -> ArtifactStore:
    return ArtifactStore(
        tmp_path / "artifacts",
        inline_threshold_bytes=inline,
        receipt_head_chars=20,
        receipt_tail_chars=20,
    )


def _history_with_big_output(*, body: str = "X" * 500, call_id: str = "c1") -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content="run the build")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="run_command", args={"cmd": "make"}, tool_call_id=call_id)]
        ),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_command", content=body, tool_call_id=call_id)]),
        ModelResponse(parts=[TextPart(content="build done")]),
    ]


# --------------------------------------------------------------------------- #
# Large output -> receipt + artifact, pairing preserved
# --------------------------------------------------------------------------- #


def test_large_output_replaced_with_receipt_and_spilled_to_artifact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    history = _history_with_big_output()
    result = reduce_tool_outputs(history, store, keep_recent_full=0)

    assert result.artifacted == 1
    assert result.reduced == 1
    assert len(result.receipts) == 1
    receipt = result.receipts[0]
    assert receipt.byte_size == 500
    assert receipt.artifact_ref is not None
    assert receipt.artifact_ref.startswith("sha256:")
    # The artifact body is recoverable from the store.
    assert store.read(receipt.artifact_ref) == (b"X" * 500)
    # The model-visible content is the receipt, not the 500-byte body.
    return_part = next(
        p
        for m in result.messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    )
    assert "tool-receipt" in str(return_part.content)
    assert "X" * 500 not in str(return_part.content)


def test_reduction_preserves_tool_pairing(tmp_path: Path) -> None:
    """A reduced history still satisfies the call/return pairing invariants."""

    store = _store(tmp_path)
    history = _history_with_big_output()
    result = reduce_tool_outputs(history, store, keep_recent_full=0)
    assert validate_active_history(result.messages) == []


def test_recent_messages_kept_full(tmp_path: Path) -> None:
    """keep_recent_full trailing messages are not receipt-ized."""

    store = _store(tmp_path)
    history = _history_with_big_output()
    result = reduce_tool_outputs(history, store, keep_recent_full=1)
    assert result.reduced == 1  # the big return is outside the 1-message tail
    # With keep_recent_full=3 the whole turn is kept verbatim.
    result_full = reduce_tool_outputs(history, store, keep_recent_full=3)
    assert result_full.reduced == 0
    assert result_full.messages == history


# --------------------------------------------------------------------------- #
# Empty and duplicate outputs
# --------------------------------------------------------------------------- #


def test_empty_output_replaced_with_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    history = [
        ModelRequest(parts=[UserPromptPart(content="grep nothing")]),
        ModelResponse(parts=[ToolCallPart(tool_name="search", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="search", content="", tool_call_id="c1")]),
    ]
    result = reduce_tool_outputs(history, store, keep_recent_full=0)
    assert result.reduced == 1
    assert result.receipts[0].summary == "search returned empty output"


def test_duplicate_output_dropped_to_duplicate_receipt(tmp_path: Path) -> None:
    """An output identical to an earlier one is replaced with a duplicate receipt."""

    store = _store(tmp_path, inline=1000)  # large threshold so not "large"
    body = "same output body"
    history = [
        ModelRequest(parts=[UserPromptPart(content="twice")]),
        ModelResponse(parts=[ToolCallPart(tool_name="read", args={}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read", content=body, tool_call_id="c1")]),
        ModelResponse(parts=[ToolCallPart(tool_name="read", args={}, tool_call_id="c2")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read", content=body, tool_call_id="c2")]),
    ]
    result = reduce_tool_outputs(history, store, keep_recent_full=0)
    # The second identical output is a duplicate (no artifact, content dropped).
    assert result.reduced >= 1
    dup_receipts = [r for r in result.receipts if "duplicate" in r.summary]
    assert dup_receipts
    assert all(r.artifact_ref is None for r in dup_receipts)


# --------------------------------------------------------------------------- #
# never policy (secret-bearing) redacts without persisting
# --------------------------------------------------------------------------- #


def test_build_receipt_never_policy_redacts_and_does_not_spill(tmp_path: Path) -> None:
    store = _store(tmp_path, inline=1)
    receipt, text = build_receipt(
        store,
        tool_call_id="c1",
        tool_name="get_secret",
        content="API_KEY=sk-xxxxxxxxxxxxxxxx",
        status="success",
        artifact_policy="never",
        head_chars=20,
        tail_chars=20,
    )
    assert receipt.artifact_ref is None
    assert "redacted" in text
    assert "API_KEY" not in text
    # Nothing was written to disk.
    assert not list((tmp_path / "artifacts").glob("*")) if (tmp_path / "artifacts").exists() else True


def test_build_receipt_error_status_surfaces_error_line(tmp_path: Path) -> None:
    """Extractive reduction: an error receipt's summary includes the error line."""

    store = _store(tmp_path, inline=1)
    body = "compiling...\ncompiling...\nError: undefined symbol 'foo'\nbuild failed"
    receipt, _ = build_receipt(
        store,
        tool_call_id="c1",
        tool_name="run_command",
        content=body,
        status="error",
        head_chars=20,
        tail_chars=20,
    )
    assert "Error: undefined symbol" in receipt.summary


# --------------------------------------------------------------------------- #
# Non-tool messages untouched
# --------------------------------------------------------------------------- #


def test_user_and_assistant_text_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    history = [
        ModelRequest(parts=[UserPromptPart(content="hello")]),
        ModelResponse(parts=[TextPart(content="hi")]),
    ]
    result = reduce_tool_outputs(history, store, keep_recent_full=0)
    assert result.reduced == 0
    assert result.messages == history
