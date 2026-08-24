from __future__ import annotations

from lumen.ui.diff_view import (
    MAX_DIFF_LINES,
    style_diff_lines,
    style_diff_text,
    unified_diff_lines,
)


def test_unified_diff_lines_drops_file_headers_and_marks_changes() -> None:
    lines = unified_diff_lines("old\nsame\n", "new\nsame\n")

    assert not any(line.startswith(("---", "+++")) for line in lines)
    assert "-old" in lines
    assert "+new" in lines
    assert any(line.startswith("@@") for line in lines)


def test_unified_diff_lines_empty_for_identical_input() -> None:
    assert unified_diff_lines("same\n", "same\n") == []


def test_unified_diff_lines_caps_output() -> None:
    old = "\n".join(f"old {i}" for i in range(MAX_DIFF_LINES + 50))
    new = "\n".join(f"new {i}" for i in range(MAX_DIFF_LINES + 50))

    lines = unified_diff_lines(old, new)

    assert len(lines) == MAX_DIFF_LINES + 1
    assert lines[-1].startswith("… diff truncated")


def test_style_diff_lines_colors_add_remove_hunk_and_context() -> None:
    text = style_diff_lines(
        ["@@ -1 +1 @@", "-gone", "+here", " ctx"],
        add_color="green",
        del_color="red",
    )

    styles = {span.style for span in text.spans}
    assert "dim italic" in styles
    assert "red" in styles
    assert "green" in styles
    assert "dim" in styles


def test_style_diff_text_keeps_file_headers_dim_not_colored() -> None:
    text = style_diff_text(
        "--- a.py (before)\n+++ a.py (after)\n-old\n+new\n",
        add_color="green",
        del_color="red",
    )

    header_spans = [
        span for span in text.spans if text.plain[span.start : span.end].startswith(("---", "+++"))
    ]
    assert header_spans
    assert all(span.style == "dim" for span in header_spans)
    change_styles = {
        span.style
        for span in text.spans
        if text.plain[span.start : span.end].startswith(("-old", "+new"))
    }
    assert change_styles == {"red", "green"}
