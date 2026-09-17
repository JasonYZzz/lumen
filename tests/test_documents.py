from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lumen.files.documents import MAX_DOCUMENT_BYTES, read_workspace_document


def test_secure_document_read_fails_closed_without_posix_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from lumen.files import documents

    (tmp_path / "report.md").write_text("private", encoding="utf-8")
    monkeypatch.setattr(documents, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="supported on macOS and Linux"):
        read_workspace_document(tmp_path, "report.md")


def test_document_read_is_bounded_and_rejects_escapes(tmp_path: Path) -> None:
    (tmp_path / "report.md").write_text("# 报告", encoding="utf-8")
    if sys.platform == "win32":
        with pytest.raises(ValueError, match="supported on macOS and Linux"):
            read_workspace_document(tmp_path, "report.md")
        return
    assert read_workspace_document(tmp_path, "report.md") == "# 报告".encode()
    for path in ("../report.md", ".env", str(tmp_path / "report.md"), "a/../report.md"):
        with pytest.raises(ValueError):
            read_workspace_document(tmp_path, path)
    (tmp_path / "link.md").symlink_to(tmp_path / "report.md")
    with pytest.raises(ValueError):
        read_workspace_document(tmp_path, "link.md")
    (tmp_path / "folder").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        read_workspace_document(tmp_path, "folder/report.md")
    os.mkfifo(tmp_path / "pipe.txt")
    with pytest.raises(ValueError, match="regular"):
        read_workspace_document(tmp_path, "pipe.txt")
    with (tmp_path / "large.txt").open("wb") as stream:
        stream.truncate(MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(ValueError, match="20 MiB"):
        read_workspace_document(tmp_path, "large.txt")
