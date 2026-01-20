"""CLI entrypoint for LLM proxy service."""

from __future__ import annotations

import os
from typing import Annotated

import typer
import uvicorn

cli = typer.Typer(help="LLM proxy service")


@cli.command()
def serve(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 5052,
    log_level: Annotated[str, typer.Option(help="Log level")] = "info",
) -> None:
    """Start the LLM proxy server."""
    from props.llm_proxy.proxy import app

    uvicorn.run(app, host=host, port=port, log_level=log_level)


def main() -> None:
    """Main entry point."""
    # Allow port override via environment
    port = int(os.environ.get("PORT", "5052"))
    log_level = os.environ.get("LOG_LEVEL", "info")
    serve(host="0.0.0.0", port=port, log_level=log_level)


if __name__ == "__main__":
    main()
