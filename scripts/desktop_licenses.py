"""Collect the licenses of locked Python and Rust dependencies into the app resources."""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "build/desktop/backend/third-party"


def main() -> None:
    DESTINATION.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", DESTINATION / "Lumen-LICENSE.txt")
    python = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata["Name"]
        python.append({"name": name, "version": distribution.version,
                       "license": distribution.metadata.get("License-Expression")
                       or distribution.metadata.get("License", "See included license files")})
        for file in distribution.files or []:
            if file.name.lower().startswith(("license", "copying", "notice")):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    target = DESTINATION / "python" / name / file.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    data = json.loads(subprocess.check_output([
        "cargo", "metadata", "--locked", "--format-version", "1", "--filter-platform",
        "aarch64-apple-darwin", "--manifest-path", str(ROOT / "src/desktop/Cargo.toml"),
    ]))
    rust = []
    for package in data["packages"]:
        rust.append({"name": package["name"], "version": package["version"], "license": package["license"]})
        folder = Path(package["manifest_path"]).parent
        for file in folder.iterdir():
            if file.is_file() and file.name.lower().startswith(("license", "copying", "notice")):
                target = DESTINATION / "rust" / f'{package["name"]}-{package["version"]}' / file.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
    (DESTINATION / "dependencies.json").write_text(
        json.dumps({"python": python, "rust": rust}, ensure_ascii=False, indent=2), encoding="utf-8",
    )


if __name__ == "__main__":
    main()
