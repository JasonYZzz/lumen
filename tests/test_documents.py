from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lumen.files.documents import MAX_DOCUMENT_BYTES, linked_document_paths, read_workspace_document


def test_explicit_local_attachments_are_bounded_and_not_remote_or_hidden(tmp_path: Path) -> None:
    text = ("[Report](docs/report.md) ![Image](images/art.png) [Space](<docs/my report.pdf>) "
            "[Duplicate](docs/report.md) [Secret](.env) [Escape](../x.md) "
            "[URL](https://example.com/report.pdf) [Invalid](http://[broken) "
            "[Numbered](docs/report(1).html) [Nested](docs/report(a(2)).html) "
            r"[Escaped](docs/report\(3\).html) "
            f"[Absolute]({tmp_path / 'out/report.md'})")
    assert linked_document_paths(text, tmp_path) == (
        "docs/report.md", "images/art.png", "docs/my report.pdf", "docs/report(1).html",
        "docs/report(a(2)).html", "docs/report(3).html", "out/report.md",
    )
    assert len(linked_document_paths(" ".join(f"[f](file-{i}.pdf)" for i in range(100)), tmp_path)) == 32


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
    with pytest.raises(ValueError, match="not produced"):
        read_workspace_document(tmp_path, "report.md", modified_since=10**12)
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
