from pathlib import Path

import pytest

from lumen.tools.builtin import MAX_OUTPUT_BYTES, build_builtin_specs
from lumen.tools.workspace import WorkspaceViolation


def get_function(tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    specs = {spec.name: spec for spec in build_builtin_specs(tmp_path)}
    return specs[name].function


def test_read_file_returns_numbered_lines(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    result = read_file("notes.txt", start_line=2, max_lines=2)

    assert result == {
        "path": "notes.txt",
        "start_line": 2,
        "end_line": 3,
        "lines_returned": 2,
        "has_more": False,
        "next_start_line": None,
        "total_lines": 3,
        "truncated_reason": None,
        "content": "2: beta\n3: gamma",
    }


def test_read_file_returns_explicit_continuation_metadata(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    first = read_file("notes.txt", start_line=2, max_lines=2)
    second = read_file("notes.txt", start_line=first["next_start_line"], max_lines=2)

    assert first["content"] == "2: two\n3: three"
    assert first["has_more"] is True
    assert first["next_start_line"] == 4
    assert first["total_lines"] is None
    assert first["truncated_reason"] == "line_limit"
    assert second["content"] == "4: four\n5: five"
    assert second["has_more"] is False
    assert second["total_lines"] == 5


def test_read_file_reports_start_past_eof(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("one\ntwo\n", encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    result = read_file("notes.txt", start_line=10)

    assert result["content"] == ""
    assert result["end_line"] is None
    assert result["lines_returned"] == 0
    assert result["has_more"] is False
    assert result["total_lines"] == 2


def test_read_file_stops_before_byte_budget_and_can_continue(tmp_path: Path) -> None:
    long_line = "x" * 20_000
    (tmp_path / "large.txt").write_text("\n".join([long_line] * 5), encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    result = read_file("large.txt")

    assert result["lines_returned"] == 3
    assert result["has_more"] is True
    assert result["next_start_line"] == 4
    assert result["truncated_reason"] == "output_limit"
    assert len(str(result["content"]).encode("utf-8")) <= MAX_OUTPUT_BYTES


def test_read_file_rejects_a_single_line_larger_than_budget(tmp_path: Path) -> None:
    (tmp_path / "minified.json").write_text("x" * (MAX_OUTPUT_BYTES + 1), encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    with pytest.raises(ValueError, match=r"第 1 行超过.*search_text"):
        read_file("minified.json")


def test_read_file_rejects_binary_and_invalid_utf8(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"prefix\x00payload")
    (tmp_path / "invalid.txt").write_bytes(b"valid prefix\xffinvalid")
    read_file = get_function(tmp_path, "read_file")

    with pytest.raises(ValueError, match="二进制文件"):
        read_file("binary.bin")
    with pytest.raises(ValueError, match="不是有效的 UTF-8"):
        read_file("invalid.txt")


def test_read_file_binary_probe_allows_split_utf8_codepoint(tmp_path: Path) -> None:
    content = "a" * 8191 + "界\nnext\n"
    (tmp_path / "utf8.txt").write_text(content, encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    result = read_file("utf8.txt", start_line=2)

    assert result["content"] == "2: next"


def test_read_file_does_not_use_whole_file_read_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "large.txt"
    path.write_text("\n".join(str(index) for index in range(10_000)), encoding="utf-8")
    read_file = get_function(tmp_path, "read_file")

    def fail_read_text(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("read_file must stream instead of calling Path.read_text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    result = read_file("large.txt", max_lines=2)

    assert result["content"] == "1: 0\n2: 1"
    assert result["next_start_line"] == 3


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
    assert "[结果已在 1 条匹配处截断]" in result


def test_search_text_accepts_an_exact_file_path(tmp_path: Path) -> None:
    (tmp_path / "loop.py").write_text("async def run():\n    pass\n", encoding="utf-8")
    search_text = get_function(tmp_path, "search_text")

    result = search_text(r"async def (run|step)", path="loop.py")

    assert result == "loop.py:1:async def run():"


def test_search_text_skips_symlinks_that_escape_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-python"
    outside.write_text("prompts/system.md", encoding="utf-8")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(outside)
    (tmp_path / "escape.txt").symlink_to(outside)
    (tmp_path / "agent.yaml").write_text(
        "instructions_file: prompts/system.md\n",
        encoding="utf-8",
    )
    search_text = get_function(tmp_path, "search_text")

    result = search_text(r"prompts/system\.md")

    assert result == "agent.yaml:1:instructions_file: prompts/system.md"


def test_search_text_does_not_scan_ignored_directories(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "activation.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("needle\n", encoding="utf-8")
    search_text = get_function(tmp_path, "search_text")

    result = search_text("needle")

    assert result == "notes.txt:1:needle"
