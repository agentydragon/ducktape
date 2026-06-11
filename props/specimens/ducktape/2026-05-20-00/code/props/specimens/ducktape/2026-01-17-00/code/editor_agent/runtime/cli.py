"""Editor CLI tools - init bootstrap and submit commands via MCP."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from agent_pkg.runtime.mcp import mcp_client_from_env, read_text_resource
from agent_pkg.runtime.output import render_agent_prompt
from cli_util.decorators import async_run
from cli_util.logging import LogLevel, make_logging_callback
from editor_agent.runtime import EDIT_RESOURCE_URI, PROMPT_RESOURCE_URI

submit_app = typer.Typer(name="editor-submit", help="Editor submit helper for MCP communication")
submit_app.callback()(make_logging_callback(default_level=LogLevel.WARNING))


async def _get_filename() -> str:
    """Read target filename from resource metadata."""
    async with mcp_client_from_env() as (client, _init_result):
        resources = await client.list_resources()
        for resource in resources:
            if str(resource.uri) == EDIT_RESOURCE_URI:
                return resource.name
        raise RuntimeError(f"Resource {EDIT_RESOURCE_URI} not found")


@submit_app.command("read-input")
@async_run
async def read_input() -> None:
    """Read the original file content from the MCP server."""
    async with mcp_client_from_env() as (client, _init_result):
        content = await read_text_resource(client, EDIT_RESOURCE_URI)
    sys.stdout.write(content)


@submit_app.command("read-prompt")
@async_run
async def read_prompt() -> None:
    """Read the edit instructions from the MCP server."""
    async with mcp_client_from_env() as (client, _init_result):
        prompt = await read_text_resource(client, PROMPT_RESOURCE_URI)
    sys.stdout.write(prompt)


@submit_app.command("materialize")
@async_run
async def materialize(
    directory: Annotated[Path, typer.Argument(help="Directory to write file to")] = Path("/workspace"),
) -> None:
    """Materialize the target file to disk and print its path."""
    async with mcp_client_from_env() as (client, _init_result):
        filename = await _get_filename()
        content = await read_text_resource(client, EDIT_RESOURCE_URI)
    target_path = directory / filename
    target_path.write_text(content, encoding="utf-8")
    print(target_path)


@submit_app.command("submit-success")
@async_run
async def submit_success(
    message: Annotated[str, typer.Option("--message", "-m", help="Success message")],
    file: Annotated[Path, typer.Option("--file", "-f", help="Path to file with edited content")],
) -> None:
    """Submit successful edit with the file content."""
    content = file.read_text(encoding="utf-8")
    async with mcp_client_from_env() as (client, _init_result):
        await client.call_tool("submit_success", {"message": message, "content": content})


@submit_app.command("submit-failure")
@async_run
async def submit_failure(message: Annotated[str, typer.Option("--message", "-m", help="Failure message")]) -> None:
    """Submit failure with a message."""
    async with mcp_client_from_env() as (client, _init_result):
        await client.call_tool("submit_failure", {"message": message})


@submit_app.command("init")
def init_cmd() -> None:
    """Bootstrap the editor agent environment."""
    render_agent_prompt("editor_agent/runtime/docs/agent.md")


def main() -> None:
    """Entry point for the editor-submit CLI."""
    submit_app()


if __name__ == "__main__":
    main()
