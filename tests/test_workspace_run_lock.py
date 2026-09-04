from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lumen.application.run_lock import WorkspaceRunLock


def test_workspace_run_lock_rejects_second_process_and_releases_after_exit(tmp_path: Path) -> None:
    script = """
import sys
import time
from pathlib import Path
from lumen.application.run_lock import WorkspaceRunLock

lock = WorkspaceRunLock(Path(sys.argv[1]))
if not lock.acquire():
    raise SystemExit(2)
print("acquired", flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "acquired"

        contender = WorkspaceRunLock(tmp_path)
        assert contender.acquire() is False

        process.terminate()
        process.wait(timeout=10)
        assert contender.acquire() is True
        contender.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
