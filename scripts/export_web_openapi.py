"""Export the FastAPI contract consumed by the static Web client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from lumen.api import create_web_app
from lumen.application import WorkspaceHost


def main() -> None:
    # Route registration does not access the host; it is only needed when the
    # ASGI lifespan or a request runs. Keeping schema export independent from
    # project configuration makes contract checks deterministic in CI.
    app = create_web_app(cast(WorkspaceHost, object()), launch_token="schema", api_only=True)
    target = Path(__file__).resolve().parents[1] / "src" / "web" / "openapi.json"
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
