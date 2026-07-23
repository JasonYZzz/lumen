from __future__ import annotations

from lumen.ui.composer import ComposerHistory, ComposerState, is_large_paste


def test_large_paste_uses_marker_and_expands_before_submission() -> None:
    pasted = "\n".join(f"line {index}" for index in range(12))
    state = ComposerState()

    marker = state.compact_paste(pasted)

    assert marker.startswith("[pasted 12 lines /")
    assert state.expand(f"before\n{marker}\nafter") == f"before\n{pasted}\nafter"


def test_small_and_large_paste_thresholds() -> None:
    assert is_large_paste("one\ntwo") is False
    assert is_large_paste("\n".join("x" for _ in range(8))) is True
    assert is_large_paste("x" * 4096) is True


def test_clear_submitted_keeps_kill_ring_and_restorable_pastes() -> None:
    state = ComposerState(draft="draft", kill_ring="killed")
    marker = state.compact_paste("large text")

    state.clear_submitted()

    assert state.draft == ""
    assert state.kill_ring == "killed"
    assert state.expand(marker) == "large text"


def test_history_restores_unsent_draft_after_newest_entry() -> None:
    history = ComposerHistory(max_entries=2)
    history.record("first")
    history.record("second")

    older = history.navigate("unsent draft", -1)
    oldest = history.navigate("second", -1)
    newer = history.navigate("first", 1)
    restored = history.navigate("second", 1)

    assert older is not None and older.text == "second"
    assert oldest is not None and oldest.text == "first"
    assert newer is not None and newer.text == "second"
    assert restored is not None and restored.text == "unsent draft"
    assert restored.browsing is False
