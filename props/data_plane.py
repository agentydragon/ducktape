"""Scaffold shared by the standalone data-plane proxies, `props.llm_proxy` and `props.registry_proxy`."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from props.db.async_database import AsyncDatabase
from props.db.config import DatabaseConfig
from util.logging import LogLevel, configure_logging

configure_logging(
    log_output=os.environ.get("PROPS_LOG_OUTPUT", "stderr"), log_level=os.environ.get("PROPS_LOG_LEVEL", LogLevel.INFO)
)


@asynccontextmanager
async def admin_database(db: AsyncDatabase | None) -> AsyncIterator[AsyncDatabase]:
    """Yield the injected `db` (tests), else open and verify the `PG*`-configured one for the app's lifetime."""
    if db is not None:
        yield db
        return
    async with AsyncDatabase(DatabaseConfig()) as opened:
        await opened.verify()
        yield opened


def add_probe_routes(app: FastAPI) -> None:
    """Serve `/health` and `/readyz`. Startup is only DB and config wiring, so readiness equals liveness."""

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse)
    async def readyz() -> str:
        return "ok"


def create_serve_cli(*, name: str, create_app: Callable[[], FastAPI]) -> typer.Typer:
    """Build the `serve` CLI that the proxy image entrypoint runs as `serve --host 0.0.0.0`."""
    cli = typer.Typer(help=f"props {name}")

    # A callback forces typer to treat `serve` as a required subcommand. Without it a
    # single-command Typer collapses into a no-subcommand app and rejects the `serve`
    # argument the image entrypoint passes ("Got unexpected extra argument (serve)").
    @cli.callback()
    def _root() -> None:
        """Group callback."""

    @cli.command(help=f"Start the {name} server.")
    def serve(
        host: Annotated[str, typer.Option(help="Host to bind to")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="Port to bind to")] = 8000,
    ) -> None:
        uvicorn.run(create_app(), host=host, port=port)

    return cli
