"""Build the open-source, ad-hoc signed Apple Silicon application without publishing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "desktop"
PYTHON = "3.13.15"


def run(*args: str, environ: dict[str, str] | None = None, cwd: Path = ROOT) -> None:
    subprocess.run(args, cwd=cwd, env=environ, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-only", action="store_true")
    parser.add_argument("--skip-web", action="store_true", help="Use an already built Web export")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        parser.error("Build on an Apple Silicon Mac; cross-compilation is not supported")
    if not args.skip_web:
        run("corepack", "pnpm@10.28.2", "--dir", "src/web", "install", "--frozen-lockfile")
        run("corepack", "pnpm@10.28.2", "--dir", "src/web", "build")
    if not (ROOT / "src/web/out/index.html").is_file():
        parser.error("Build Web assets first")
    environment = dict(os.environ)
    environment["UV_PROJECT_ENVIRONMENT"] = str(BUILD / ".venv")
    run("uv", "sync", "--frozen", "--python", PYTHON, "--no-default-groups", "--group", "desktop",
        "--extra", "web", "--extra", "live", "--extra", "tokenizers", environ=environment)
    python = BUILD / ".venv/bin/python"
    run("uv", "build", "--wheel", "--out-dir", str(BUILD / "wheel"), "--python", str(python))
    wheels = list((BUILD / "wheel").glob("*.whl"))
    if len(wheels) != 1:
        parser.error("Expected exactly one wheel; remove stale build/desktop/wheel artifacts")
    run("uv", "pip", "install", "--no-deps", "--python", str(python), str(wheels[0]))
    run(str(BUILD / ".venv/bin/lumen"), "--version", cwd=BUILD)
    with tempfile.TemporaryDirectory(prefix="lumen-wheel-check-") as temporary:
        check_root = Path(temporary)
        config = check_root / "agent.yaml"
        config.write_text("version: 2\nagent: {model: {id: test}}\ntools: {builtins: []}\n")
        run(str(BUILD / ".venv/bin/lumen"), "--cwd", temporary, "--config", str(config),
            "--check-config", environ={"HOME": temporary, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
            cwd=check_root)
    tokenizer_environment = dict(os.environ)
    tokenizer_environment["TIKTOKEN_CACHE_DIR"] = str(BUILD / "tokenizers")
    run(str(python), "-c",
        "import tiktoken; [tiktoken.get_encoding(n) for n in tiktoken.list_encoding_names()]",
        environ=tokenizer_environment)
    run(str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(BUILD),
        "--workpath", str(BUILD / "pyinstaller"), str(ROOT / "scripts/lumen_backend.spec"))
    run(str(python), str(ROOT / "scripts/desktop_licenses.py"))
    run(str(python), str(ROOT / "scripts/smoke_desktop_backend.py"), str(BUILD / "backend/lumen-backend"))
    artifacts: dict[str, str] = {}
    if not args.backend_only:
        run("corepack", "pnpm@10.28.2", "--dir", "src/desktop", "install", "--frozen-lockfile")
        run("corepack", "pnpm@10.28.2", "--dir", "src/desktop", "tauri", "build", "--target",
            "aarch64-apple-darwin", "--config", "tauri.bundle.json", "--bundles", "app,dmg", "--", "--locked")
        app = ROOT / "src/desktop/target/aarch64-apple-darwin/release/bundle/macos/Lumen.app"
        binaries = 0
        for path in app.rglob("*"):
            if not path.is_file():
                continue
            with path.open("rb") as file:
                magic = file.read(4)
            if magic in {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                         b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}:
                architecture = subprocess.check_output(["lipo", "-archs", str(path)], text=True).strip()
                if architecture != "arm64":
                    raise RuntimeError(f"Non-arm64 Mach-O in bundle: {path.relative_to(app)}")
                binaries += 1
        assert binaries >= 2, "Desktop shell and Python backend must both be present"
        run("codesign", "--verify", "--deep", "--strict", str(app))
        run(str(python), str(ROOT / "scripts/smoke_desktop_backend.py"),
            str(app / "Contents/Resources/backend/lumen-backend"))
        version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        dmg = app.parent.parent / "dmg" / f"Lumen_{version}_aarch64.dmg"
        run("hdiutil", "verify", str(dmg))
        artifacts[dmg.name] = hashlib.sha256(dmg.read_bytes()).hexdigest()
        (dmg.parent / "SHA256SUMS.txt").write_text(f"{artifacts[dmg.name]}  {dmg.name}\n")
    locks = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (ROOT / "uv.lock", ROOT / "src/web/pnpm-lock.yaml",
                          ROOT / "src/desktop/Cargo.lock", ROOT / "src/desktop/pnpm-lock.yaml")}
    (BUILD / "manifest.json").write_text(json.dumps({"python": PYTHON, "architecture": "arm64",
        "macos": platform.mac_ver()[0], "signing": "ad-hoc", "notarized": False,
        "locks": locks, "artifacts": artifacts}, indent=2))


if __name__ == "__main__":
    main()
