"""LLM proxy entrypoint: `serve` starts the uvicorn server."""

from __future__ import annotations

from typing import Annotated

import typer
import uvicorn

from props.llm_proxy.app import create_app

cli = typer.Typer(help="props LLM proxy")


# A callback forces typer to treat `serve` as a required subcommand. Without it a
# single-command Typer collapses into a no-subcommand app and rejects the `serve`
# argument the image entrypoint passes ("Got unexpected extra argument (serve)").
@cli.callback()
def _root() -> None:
    """props LLM proxy — the agent data plane."""


@cli.command()
def serve(
    host: Annotated[str, typer.Option(help="Host to bind to")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind to")] = 8000,
) -> None:
    """Start the LLM proxy server."""
    uvicorn.run(create_app(), host=host, port=port)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
