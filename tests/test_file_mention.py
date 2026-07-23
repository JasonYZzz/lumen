"""Tests for @path file mention expansion.

These pin down the contract: @path is replaced with a <file path="...">
content </file> block, workspace escapes are reported but don't abort the
prompt, and bare @ characters (emails, mentions) are left alone.
"""

from __future__ import annotations

from pathlib import Path

from lumen.tools.workspace import Workspace
from lumen.ui.file_mention import expand_file_mentions


def test_expand_relative_path_injects_content(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("hello: world\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions("please read @config.yaml and explain", ws)
    assert '<file path="config.yaml">' in out
    assert "hello: world" in out
    assert "</file>" in out
    # Surrounding prose preserved.
    assert "please read" in out
    assert "and explain" in out


def test_expand_missing_path_reports_not_found(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    out = expand_file_mentions("look at @does-not-exist.txt", ws)
    assert '<file path="does-not-exist.txt" missing>' in out
    assert "not found" in out.lower()


def test_expand_workspace_escape_is_caught_not_raised(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("ok", encoding="utf-8")
    ws = Workspace(tmp_path)
    # WorkspaceViolation should become a <file missing> block, not raise.
    out = expand_file_mentions("escape @../etc/passwd", ws)
    assert "<file" in out
    assert "missing" in out
    # The original prompt isn't broken — surrounding text survives.
    assert "escape" in out


def test_bare_at_sign_left_alone(tmp_path: Path) -> None:
    """Email-style @ and lone @ without a path char following are not expanded."""
    ws = Workspace(tmp_path)
    text = "contact me at user@example.com or ping @here"
    out = expand_file_mentions(text, ws)
    # No <file> blocks were injected.
    assert "<file" not in out
    assert "user@example.com" in out
    assert "@here" in out


def test_multiple_mentions_all_expand(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions("compare @a.txt and @b.txt", ws)
    assert "alpha" in out
    assert "beta" in out
    assert out.count("<file") == 2


def test_nested_relative_path(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "mod.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("x = 1", encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions("see @src/mod.py", ws)
    assert '<file path="src/mod.py">' in out
    assert "x = 1" in out


def test_large_file_is_truncated(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * (100 * 1024), encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions("@big.txt", ws)
    # Truncation marker present; total well under 100 KiB.
    assert "truncated" in out.lower()
    assert len(out) < 100 * 1024


def test_binary_file_injects_metadata_not_content(tmp_path: Path) -> None:
    (tmp_path / "image.bin").write_bytes(b"prefix\x00secret-binary-payload")
    ws = Workspace(tmp_path)

    out = expand_file_mentions("inspect @image.bin", ws)

    assert "binary file omitted" in out
    assert "secret-binary-payload" not in out


def test_quoted_path_with_spaces(tmp_path: Path) -> None:
    (tmp_path / "my file.txt").write_text("spaced", encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions('read @"my file.txt"', ws)
    assert '<file path="my file.txt">' in out
    assert "spaced" in out


def test_mention_at_end_of_string(tmp_path: Path) -> None:
    (tmp_path / "end.txt").write_text("tail", encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions("see @end.txt", ws)
    assert "<file" in out
    assert "tail" in out


def test_mention_without_trailing_space_still_matches(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("X", encoding="utf-8")
    ws = Workspace(tmp_path)
    out = expand_file_mentions("@x.txt.", ws)
    assert '<file path="x.txt">' in out
    assert "X" in out
    # The trailing period after the mention survives.
    assert out.rstrip().endswith(".")


def test_empty_text_unchanged(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    assert expand_file_mentions("", ws) == ""


def test_text_with_no_mentions_unchanged(tmp_path: Path) -> None:
    ws = Workspace(tmp_path)
    text = "just a plain question with no mentions"
    assert expand_file_mentions(text, ws) == text


# Defensive: the workspace.root absolute expansion path
def test_absolute_path_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "abs.txt").write_text("absolute", encoding="utf-8")
    ws = Workspace(tmp_path)
    abs_path = str(tmp_path / "abs.txt")
    out = expand_file_mentions(f"see @{abs_path}", ws)
    assert "absolute" in out
