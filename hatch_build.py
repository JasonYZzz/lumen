"""Conditionally bundle the pre-built Web export in release artifacts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class LumenBuildHook(BuildHookInterface):
    """Keep source installs working before Node assets have been built."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version
        if os.environ.get("LUMEN_BUILD_SKIP_WEB") == "1":
            return
        web_export = Path(self.root) / "src" / "web" / "out"
        if not (web_export / "index.html").is_file():
            return
        destination = "lumen/api/static" if self.target_name == "wheel" else "src/web/out"
        build_data["force_include"][str(web_export)] = destination
