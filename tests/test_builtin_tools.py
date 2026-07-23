from pathlib import Path

import pytest

from lumen.tools.builtin import build_builtin_specs
from lumen.tools.workspace import WorkspaceViolation


def get_function(tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    specs = {spec.name: spec for spec in build_builtin_specs(tmp_path)}
    return specs[name].function


def test_read_file_returns_numbered_lines(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    result = read_file("notes.txt", start_line=2, max_lines=2)

    assert result == "2: beta\n3: gamma"


def test_tools_reject_parent_traversal(tmp_path: Path) -> None:
    read_file = get_function(tmp_path, "read_file")

    with pytest.raises(WorkspaceViolation, match="workspace"):
        read_file("../secret.txt")


def test_tools_reject_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-lumen.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    read_file = get_function(tmp_path, "read_file")

    with pytest.raises(WorkspaceViolation, match="workspace"):
        read_file("escape.txt")


def test_search_text_limits_results(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle one\nneedle two\n", encoding="utf-8")
    search_text = get_function(tmp_path, "search_text")

    result = search_text("needle", path=".", max_results=1)

    assert result.startswith("a.txt:1:needle one")
    assert "[results truncated at 1 matches]" in result
