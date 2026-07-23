from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from lumen.branding import FRAMEWORK_NAME, FRAMEWORK_SLUG
from lumen.config import ConfigLoadError, load_config
from lumen.resources import ResourceManager
from lumen.ui.app import LumenApp

app = typer.Typer(
    name=FRAMEWORK_SLUG,
    help=f"Run {FRAMEWORK_NAME}, a configurable tool-using agent framework, in a terminal UI.",
    add_completion=False,
    invoke_without_command=True,
)


async def _check_resources(manager: ResourceManager) -> dict[str, object]:
    async with manager:
        return manager.summary()


@app.callback()
def main(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to agent YAML configuration."),
    ] = Path("agent.yaml"),
    cwd: Annotated[
        Path,
        typer.Option("--cwd", help="Workspace root exposed to built-in file tools."),
    ] = Path("."),
    resume: Annotated[
        str | None,
        typer.Option("--resume", help="Session UUID to resume at startup."),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help="Logical model name to select at startup (must be a key under agent.models).",
        ),
    ] = None,
    check_config: Annotated[
        bool,
        typer.Option("--check-config", help="Validate configuration and discover tools, then exit."),
    ] = False,
) -> None:
    """Launch Lumen or validate its complete runtime configuration."""
    try:
        workspace = cwd.expanduser().resolve()
        if not workspace.is_dir():
            raise ConfigLoadError(f"workspace is not a directory: {workspace}")
        config = load_config(config_path)
        manager = ResourceManager(config, workspace=workspace)
        if model is not None:
            # Validate up front so a typo fails fast rather than after the TUI
            # mounts. ``select_model`` would also raise, but doing it here
            # keeps the error message attached to the CLI surface.
            try:
                manager.set_startup_model(model)
            except KeyError as error:
                raise ConfigLoadError(str(error)) from error
        if check_config:
            summary = asyncio.run(_check_resources(manager))
            typer.echo("Configuration OK")
            typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        LumenApp(config, manager, resume_id=resume).run()
    except (ConfigLoadError, OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from error


if __name__ == "__main__":
    app()
