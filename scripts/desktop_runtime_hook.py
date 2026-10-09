"""Use tokenizer data shipped with the app; no first-run network download."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(sys._MEIPASS) / "tiktoken-cache")  # type: ignore[attr-defined]
