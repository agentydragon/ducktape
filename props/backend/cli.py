"""CLI for props dashboard backend."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from cli_util.logging import LogLevel, make_logging_callback
from props.backend.app import create_app, default_deps

cli = typer.Typer(help="Props dashboard backend")
cli.callback()(make_logging_callback(default_level=LogLevel.INFO))


@cli.command()
def serve(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 8000,
    reload: Annotated[bool, typer.Option(help="Enable auto-reload for development")] = False,
    reload_dir: Annotated[list[str] | None, typer.Option(help="Directories to watch for reload")] = None,
    static_dir: Annotated[Path | None, typer.Option(help="Directory with static frontend assets")] = None,
) -> None:
    """Start the props dashboard server."""
    if static_dir:
        os.environ["PROPS_DASHBOARD_STATIC_DIR"] = str(static_dir.absolute())

    app = create_app(deps=default_deps(), static_dir=static_dir)
    uvicorn.run(app, host=host, port=port, reload=reload, reload_dirs=reload_dir)


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
