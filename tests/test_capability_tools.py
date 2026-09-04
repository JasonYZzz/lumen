from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lumen.context.artifacts import ArtifactStore
from lumen.sessions import SessionRepository
from lumen.tools.capability import build_capability_specs
from lumen.tools.workspace import WorkspaceViolation
from lumen.work_products import EffectStatus, TaskWorkspace


def capability(tmp_path: Path, name: str) -> Callable[..., Any]:
    specs = build_capability_specs(tmp_path, max_timeout=2.0)
    return next(spec.function for spec in specs if spec.name == name)


def test_write_requires_explicit_overwrite(tmp_path: Path) -> None:
    write_file = capability(tmp_path, "write_file")
    (tmp_path / "note.txt").write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        write_file("note.txt", "new")
    assert write_file("note.txt", "new", overwrite=True) == "Wrote 3 bytes to note.txt"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "new"


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    write_file = capability(tmp_path, "write_file")
    write_file("nested/dir/file.txt", "hello")
    assert (tmp_path / "nested" / "dir" / "file.txt").read_text(encoding="utf-8") == "hello"


def test_atomic_write_retries_partial_os_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lumen.tools import capability as capability_module

    real_write = capability_module.os.write
    write_sizes: list[int] = []

    def partial_write(descriptor: int, data: bytes | bytearray | memoryview) -> int:
        chunk_size = max(1, len(data) // 3)
        write_sizes.append(chunk_size)
        return real_write(descriptor, data[:chunk_size])

    monkeypatch.setattr(capability_module.os, "write", partial_write)
    content = "partial-write-safe" * 100

    capability(tmp_path, "write_file")("complete.txt", content)

    assert len(write_sizes) > 1
    assert (tmp_path / "complete.txt").read_text(encoding="utf-8") == content


def test_write_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-write.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    write_file = capability(tmp_path, "write_file")
    with pytest.raises(WorkspaceViolation):
        write_file("escape.txt", "new", overwrite=True)


def test_write_rejects_parent_traversal(tmp_path: Path) -> None:
    write_file = capability(tmp_path, "write_file")
    with pytest.raises(WorkspaceViolation):
        write_file("../escape.txt", "x")


def test_edit_requires_exactly_one_match(tmp_path: Path) -> None:
    edit_file = capability(tmp_path, "edit_file")
    path = tmp_path / "code.py"
    path.write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="2 matches"):
        edit_file("code.py", "x = 1", "x = 2")
    assert path.read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_edit_replaces_unique_match(tmp_path: Path) -> None:
    edit_file = capability(tmp_path, "edit_file")
    path = tmp_path / "code.py"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    message = edit_file("code.py", "alpha", "gamma")
    assert path.read_text(encoding="utf-8") == "gamma\nbeta\n"
    assert "1 occurrence" in message


def test_edit_rejects_missing_match(tmp_path: Path) -> None:
    edit_file = capability(tmp_path, "edit_file")
    path = tmp_path / "code.py"
    path.write_text("alpha\n", encoding="utf-8")
    with pytest.raises(ValueError, match="0 matches"):
        edit_file("code.py", "missing", "x")
    assert path.read_text(encoding="utf-8") == "alpha\n"


@pytest.mark.parametrize(
    ("original", "find", "replacement"),
    [
        ("prefix\nbase64\n", "base64\n", "base64suffix\n"),
        ("prefix\n  value \t\nend\n", "  value \t\n", "  changed\n"),
        ("prefix\r\nvalue\r\nend\r\n", "value\r\n", "changed\r\n"),
        ("prefix\n  \nend\n", "  \n", "\t\n"),
    ],
)
def test_journaled_edit_preserves_exact_anchor_bytes(
    tmp_path: Path, original: str, find: str, replacement: str,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    session = repository.create(agent_name="test", model_id="test")
    workspace = TaskWorkspace(tmp_path, ArtifactStore(tmp_path / "artifacts"), repository)
    workspace.bind_session(session.id)
    path = tmp_path / "data.txt"
    path.write_bytes(original.encode())
    specs = build_capability_specs(tmp_path, max_timeout=2, task_workspace=workspace)
    edit = next(spec.function for spec in specs if spec.name == "edit_file")

    edit("data.txt", find, replacement)

    assert path.read_bytes() == original.replace(find, replacement, 1).encode()
    effects = repository.load(session.id).work_state.effects
    assert len(effects) == 1
    assert effects[0].status is EffectStatus.VERIFIED
    assert not workspace.completion_blockers(session.id)


async def test_run_command_captures_exit_code_without_shell(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    result = await run_command(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"]
    )
    assert result["exit_code"] == 3
    assert result["stdout"] == "out\n"
    assert result["stderr"] == "err\n"
    assert result["timed_out"] is False


async def test_run_command_runs_argv_not_shell(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    # A literal shell metacharacter must be passed through as an argv element,
    # never evaluated by a shell. The python -c program prints argv back so we
    # can observe whether the shell would have intercepted it.
    result = await run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", "foo; rm bar"]
    )
    assert result["exit_code"] == 0
    assert result["stdout"] == "foo; rm bar"
    assert result["timed_out"] is False


async def test_run_command_rejects_cwd_escape(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    with pytest.raises(WorkspaceViolation):
        await run_command([sys.executable, "-V"], cwd="..")


async def test_run_command_terminates_process_group_on_timeout(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    result = await run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=0.2,
    )
    assert result["timed_out"] is True
    # The process group should be reaped: a negative exit code means the child
    # was killed by a signal rather than running to completion.
    assert isinstance(result["exit_code"], int)
    assert result["exit_code"] < 0


async def test_run_command_clamps_timeout_to_max(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    # Request an absurdly large timeout; the function should clamp to max and
    # still finish quickly for a fast command.
    result = await run_command([sys.executable, "-V"], timeout=9999.0)
    assert result["exit_code"] == 0
    assert result["elapsed_seconds"] < 5


async def test_run_command_truncates_large_output(tmp_path: Path) -> None:
    run_command = capability(tmp_path, "run_command")
    # 1 MiB of output should be truncated: head + tail are kept, with a
    # truncation marker in between, never the full buffer.
    result = await run_command([sys.executable, "-c", "import sys; sys.stdout.write('x' * (1024 * 1024))"])
    assert result["stdout_truncated"] is True
    # Kept output is bounded to roughly head + tail + marker.
    assert len(result["stdout"].encode("utf-8")) <= 2 * 64 * 1024 + 400
    assert "truncated" in result["stdout"]
    assert result["stdout_total_bytes"] == 1024 * 1024


async def test_run_command_bounded_output_keeps_head_and_tail(tmp_path: Path) -> None:
    """Bounded output retains a head AND tail so both the start and end of a
    long log remain visible, plus reports total bytes read."""
    run_command = capability(tmp_path, "run_command")
    # Print many numbered lines so output exceeds the 64 KiB head/tail cap.
    script = "import sys\nfor i in range(20000):\n    sys.stdout.write(f'LINE-{i:05d}\\n')\n"
    result = await run_command([sys.executable, "-c", script])
    assert result["stdout_truncated"] is True
    # The head is preserved...
    assert "LINE-00000" in result["stdout"]
    # ...and so is the tail (the most recent output, often the real signal).
    assert "LINE-19999" in result["stdout"]
    # Total bytes read is reported even though only head+tail were kept.
    assert result["stdout_total_bytes"] > 64 * 1024
    # A middle line that fell outside head+tail is dropped (bounded).
    assert "LINE-10000" not in result["stdout"]


async def test_run_command_bounded_output_does_not_buffer_everything(tmp_path: Path) -> None:
    """A very large output must not be held in memory in full — the drain
    caps each stream to a fixed head/tail and discards the middle. We assert
    the kept output is bounded regardless of how much the child wrote."""
    run_command = capability(tmp_path, "run_command")
    # 4 MiB of output — far beyond the 64 KiB cap. The result must stay small.
    result = await run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('A' * (4 * 1024 * 1024))"]
    )
    assert result["stdout_truncated"] is True
    assert result["stdout_total_bytes"] == 4 * 1024 * 1024
    # Kept output is bounded to roughly head + tail + suffix.
    assert len(result["stdout"].encode("utf-8")) <= 2 * 64 * 1024 + 400


def test_build_capability_specs_has_expected_risks(tmp_path: Path) -> None:
    specs = {spec.name: spec for spec in build_capability_specs(tmp_path, max_timeout=1.0)}
    assert specs["write_file"].risk.value == "write"
    assert specs["edit_file"].risk.value == "write"
    assert specs["run_command"].risk.value == "execute"
    assert specs["run_command"].timeout == 1.0
    assert "outputs/<descriptive-name>" in (specs["write_file"].function.__doc__ or "")
