# Build only from the isolated, wheel-installed desktop environment.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = collect_data_files("lumen")
datas += [(str(root / "build/desktop/tokenizers"), "tiktoken-cache")]
for package in ("pydantic_ai", "genai_prices", "fastmcp", "trafilatura", "certifi", "tiktoken"):
    datas += collect_data_files(package)
for package in ("lumen-agent", "pydantic-ai-slim", "trafilatura", "websockets"):
    datas += copy_metadata(package, recursive=True)
hidden = collect_submodules("lumen") + collect_submodules("websockets")
hidden += ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
           "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on", "tiktoken_ext.openai_public"]
a = Analysis([str(root / "scripts/desktop_entry.py")], pathex=[], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={},
             runtime_hooks=[str(root / "scripts/desktop_runtime_hook.py")],
             excludes=["pytest", "IPython", "crawl4ai"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="lumen-backend", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True,
          target_arch="arm64", codesign_identity=None, entitlements_file=None)
collect = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="backend")
