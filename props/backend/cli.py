"""CLI for props dashboard backend."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from fastapi import FastAPI

import typer
import uvicorn

from props.backend.app import create_app, default_deps
from util.logging import LogLevel, make_logging_callback

logger = logging.getLogger(__name__)

cli = typer.Typer(help="Props dashboard backend")
cli.callback()(make_logging_callback(default_level=LogLevel.INFO))


class _GraderSpawningServer(uvicorn.Server):
    """Server subclass that spawns grader containers after startup completes.

    Avoids the chicken-and-egg problem: grader containers need the registry
    proxy (served by this same HTTP server) to resolve images, so we can
    only start them after uvicorn has bound its socket.

    Holds a direct reference to the FastAPI app (not via config.loaded_app)
    to avoid navigating uvicorn's ASGI middleware wrappers.
    """

    def __init__(self, config: uvicorn.Config, *, app: FastAPI) -> None:
        super().__init__(config)
        self._app = app

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        supervisor = getattr(self._app.state, "grader_supervisor", None)
        if supervisor is not None:
            logger.info("HTTP server ready, spawning graders for existing snapshots")
            self._spawn_task = asyncio.create_task(supervisor.spawn_existing(), name="grader-initial-spawn")
        else:
            logger.info("No grader supervisor configured, skipping grader spawn")


@cli.command()
def serve(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 8000,
    static_dir: Annotated[Path | None, typer.Option(help="Directory with static frontend assets")] = None,
) -> None:
    """Start the props dashboard server."""
    if static_dir:
        os.environ["PROPS_DASHBOARD_STATIC_DIR"] = str(static_dir.absolute())

    deps = default_deps(host=host, port=port)
    app = create_app(deps=deps, static_dir=static_dir)

    config = uvicorn.Config(app, host=host, port=port)
    server = _GraderSpawningServer(config, app=app)
    asyncio.run(server.serve())


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
