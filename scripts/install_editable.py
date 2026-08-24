#!/usr/bin/env python3
"""Install this checkout as the editable global Lumen tool.

Unlike ``uv tool install --editable .``, this entry point does not depend on
the caller's current working directory.  The repository root is resolved from
this file's location before uv is started.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    project_file = repository / "pyproject.toml"
    if not project_file.is_file():
        print(
            f"error: Lumen repository root is invalid; missing {project_file}",
            file=sys.stderr,
        )
        return 2

    uv_command = shlex.split(os.environ.get("LUMEN_UV_COMMAND", "uv"))
    if not uv_command:
        print("error: LUMEN_UV_COMMAND must not be empty", file=sys.stderr)
        return 2

    command = [
        *uv_command,
        "tool",
        "install",
        "--editable",
        "--force",
        str(repository),
    ]
    print(f"Installing editable Lumen from {repository}", flush=True)
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print(
            f"error: uv executable not found: {uv_command[0]!r}",
            file=sys.stderr,
        )
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
