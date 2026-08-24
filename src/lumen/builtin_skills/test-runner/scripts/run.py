from __future__ import annotations

import os
import subprocess
from pathlib import Path

root = Path(os.environ["LUMEN_WORKSPACE"])
if (root / "pyproject.toml").exists():
    command = ["python", "-m", "pytest", "-q"]
elif (root / "package.json").exists():
    command = ["npm", "test", "--", "--runInBand"]
elif (root / "go.mod").exists():
    command = ["go", "test", "./..."]
else:
    raise SystemExit("No supported test runner was detected")

print("command:", " ".join(command), flush=True)
raise SystemExit(subprocess.run(command, cwd=root, check=False).returncode)
