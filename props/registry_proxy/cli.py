"""Registry proxy entrypoint: ``serve`` starts the uvicorn server."""

from __future__ import annotations

from typing import Annotated

import typer
import uvicorn

from props.registry_proxy.app import create_app

cli = typer.Typer(help="props registry proxy")


@cli.callback()
def _root() -> None:
    """props registry proxy — the OCI image data plane."""


@cli.command()
def serve(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 8000,
) -> None:
    """Start the registry proxy server."""
    uvicorn.run(create_app(), host=host, port=port)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
