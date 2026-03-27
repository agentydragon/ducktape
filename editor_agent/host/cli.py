#!/usr/bin/env python3
"""Docker-based editor CLI.

Runs an LLM agent to edit a single file inside an isolated Docker container.
The agent has access to docker-exec for running commands, and submits the
edited content via the helper script which calls the host-side submit server.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

import aiodocker
import anyio
import typer

from editor_agent.host.agent_runner import run_editor_docker_agent
from editor_agent.host.runner import DEFAULT_NETWORK
from editor_agent.host.submit_server import SubmitStateFailure, SubmitStatePending, SubmitStateSuccess
from openai_utils.client_factory import build_client
from util.bazel import runfiles
from util.logging import make_logging_callback
from util.typer import async_run

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1-codex-mini")
EDITOR_IMAGE_TAG = "adgn-editor:latest"
_EDITOR_LOAD_RLOCATION = "_main/editor_agent/runtime/load.sh"

# Environment variable override for network
_ENV_NETWORK = os.getenv("ADGN_EDITOR_DOCKER_NETWORK", DEFAULT_NETWORK)

app = typer.Typer(help="Docker-based file editor with LLM agent.", invoke_without_command=True, no_args_is_help=True)

# Configure logging via shared callback (default: INFO level)
app.callback()(make_logging_callback())

MODEL_OPT = typer.Option(DEFAULT_MODEL, "--model", help="Model name (OPENAI_MODEL)")
NETWORK_OPT = typer.Option(_ENV_NETWORK, "--network", help="Docker network (ADGN_EDITOR_DOCKER_NETWORK)")
MAX_TURNS_OPT = typer.Option(40, "--max-turns", help="Maximum agent turns before abort")
VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Show agent actions in real-time")


@app.callback(invoke_without_command=True)
@async_run
async def edit(
    ctx: typer.Context,
    file: Annotated[Path | None, typer.Argument(help="Path to the file to edit")] = None,
    prompt: Annotated[str | None, typer.Argument(help="Edit instructions for the agent")] = None,
    model: str = MODEL_OPT,
    network: str = NETWORK_OPT,
    max_turns: int = MAX_TURNS_OPT,
    verbose: bool = VERBOSE_OPT,
) -> None:
    """Edit a file using an LLM agent in an isolated Docker container.

    Usage: adgn_editor_docker FILE "PROMPT"

    The agent reads the file content via MCP resource, makes edits using
    docker exec, and submits the final content. On success, the file is
    updated; on failure or abort, the file is left unchanged.
    """
    if ctx.invoked_subcommand is not None:
        return
    if file is None:
        raise typer.BadParameter("FILE argument is required")
    if prompt is None:
        raise typer.BadParameter("PROMPT argument is required")
    file = Path(await anyio.Path(file).resolve())
    if not await anyio.Path(file).is_file():
        raise typer.BadParameter(f"Not a file: {file}")

    model_client = build_client(model, enable_debug_logging=True)

    # Load editor image from Bazel oci_load target
    load_script = runfiles.get_required_path(_EDITOR_LOAD_RLOCATION)
    proc = await asyncio.create_subprocess_exec(
        load_script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to load editor image: {stderr.decode()}")

    async with aiodocker.Docker() as docker_client:
        typer.echo(f"Editing {file} with {model} (image {EDITOR_IMAGE_TAG})")

        result = await run_editor_docker_agent(
            file_path=file,
            prompt=prompt,
            docker_client=docker_client,
            model_client=model_client,
            max_turns=max_turns,
            image_id=EDITOR_IMAGE_TAG,
            network=network,
            verbose=verbose,
        )

    match result:
        case SubmitStateSuccess(message=msg):
            typer.echo(f"Success: {msg}")
        case SubmitStateFailure(message=msg):
            typer.echo(f"Failure: {msg}", err=True)
            raise typer.Exit(code=1)
        case SubmitStatePending():
            typer.echo("Agent did not submit (max turns reached or aborted).", err=True)
            raise typer.Exit(code=2)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
