#!/usr/bin/env python3


"""CLI to run the docker_exec MCP server via stdio transport."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import aiodocker
import typer
from typer_di import TyperDI

from cli_util.decorators import async_run
from mcp_infra.exec.container_session import BindMount, ContainerOptions
from mcp_infra.mcp_types import NetworkMode

from .server import ContainerExecServer

app = TyperDI(help="Run docker_exec MCP over stdio")


def _parse_labels(label_values: list[str] | None) -> dict[str, str] | None:
    if not label_values:
        return None
    labels: dict[str, str] = {}
    for raw_label in label_values:
        if "=" not in raw_label:
            raise typer.BadParameter(f"Invalid label '{raw_label}'. Expected key=value format.")
        key, value = raw_label.split("=", 1)
        labels[key] = value
    return labels


@app.command()
@async_run
async def main(
    image: Annotated[str, typer.Option(help="Docker image for session containers")],
    working_dir: Annotated[str, typer.Option(help="Working directory inside the container")] = "/workspace",
    network_mode: Annotated[NetworkMode, typer.Option(help="Docker network mode")] = NetworkMode.NONE,
    binds: Annotated[
        list[str] | None,
        typer.Option(
            help="Bind mount specification host:container[:mode]. May be supplied multiple times or as comma-separated entries."
        ),
    ] = None,
    label: Annotated[
        list[str] | None, typer.Option(help="Docker label to apply to the container (key=value). May be repeated.")
    ] = None,
) -> None:
    """Run docker_exec MCP server over stdio transport."""
    docker_client = aiodocker.Docker()
    try:
        try:
            parsed_binds = BindMount.parse_binds(binds)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
        labels_dict = _parse_labels(label)

        opts = ContainerOptions(
            image=image,
            working_dir=Path(working_dir),
            binds=parsed_binds,
            network_mode=network_mode,
            labels=labels_dict,
        )

        server = ContainerExecServer(docker_client, opts)
        await server.run_stdio_async()
    finally:
        await docker_client.close()


if __name__ == "__main__":
    app()
