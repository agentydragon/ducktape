"""CLI to run the docker_exec MCP server via stdio transport.

Accepts config as inline JSON (--config) or a JSON file path (--config-file).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import aiodocker
import typer
from typer_di import TyperDI

from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.exec.docker.types import ContainerExecServerConfig
from util.typer import async_run

app = TyperDI(help="Run docker_exec MCP over stdio")


@app.command()
@async_run
async def main(
    config: Annotated[str | None, typer.Option(help="Inline JSON config (ContainerExecServerConfig)")] = None,
    config_file: Annotated[Path | None, typer.Option(help="Path to JSON config file")] = None,
) -> None:
    """Run docker_exec MCP server over stdio transport."""
    if config is not None and config_file is not None:
        raise typer.BadParameter("Specify --config or --config-file, not both.")
    if config is not None:
        parsed = ContainerExecServerConfig.model_validate_json(config)
    elif config_file is not None:
        parsed = ContainerExecServerConfig.model_validate_json(config_file.read_text())
    else:
        raise typer.BadParameter("Specify --config or --config-file.")

    docker_client = aiodocker.Docker()
    try:
        server = ContainerExecServer(docker_client, parsed)
        await server.run_stdio_async()
    finally:
        await docker_client.close()


if __name__ == "__main__":
    app()
