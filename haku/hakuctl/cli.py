"""hakuctl — a small MCP client for the Haku console's ``/mcp`` endpoint.

Speaks streamable-HTTP MCP with a static Agent bearer (the reflected
``haku-console-agent-api`` secret) and offers the generic ``tools/list`` and
``tools/call`` surface: ``list`` the tools, read a tool's ``schema``, and
``call`` it with JSON arguments. The console keeps the approval/audit boundary;
this is only the client that builds and fires those requests from a shell.

The bearer comes from ``$HAKU_AGENT_TOKEN`` (never a flag, so it stays out of
shell history and ``ps``); the endpoint URL from ``--url`` / ``$HAKU_MCP_URL``,
defaulting to the deployed console.

TLS/proxy trust is left to the environment: ``fastmcp.Client`` builds an
``httpx.AsyncClient`` with ``trust_env`` on, so it honors ``HTTPS_PROXY`` and
the ``SSL_CERT_FILE`` CA bundle the session already exports — the same trust the
rest of the repo's HTTPS-from-CLI code relies on. No CA path is hardcoded and
verification is never disabled.
"""

from __future__ import annotations

import json
import os
from typing import Any

import typer
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp import types as mcp_types
from rich.console import Console
from typer.main import get_command

from util.typer import async_run

DEFAULT_URL = "https://haku.allegedly.works/mcp"
TOKEN_ENV = "HAKU_AGENT_TOKEN"
URL_ENV = "HAKU_MCP_URL"

app = typer.Typer(help="MCP client for the Haku console (/mcp).", no_args_is_help=True)
_out = Console()
_err = Console(stderr=True)

# Typer Option defaults must be module constants, not built in the signature (ruff B008).
URL_OPT = typer.Option(DEFAULT_URL, "--url", envvar=URL_ENV, help="haku-console MCP endpoint URL.")


def build_client(url: str, token: str) -> Client:
    """A streamable-HTTP MCP client bound to ``url`` and authenticated by ``token``.

    ``StreamableHttpTransport`` accepts the bearer as a plain string and wraps it
    as ``Authorization: Bearer …``; the underlying httpx client picks up the
    ambient proxy and CA trust (see module docstring).
    """
    return Client(StreamableHttpTransport(url, auth=token))


def _client(url: str) -> Client:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        _err.print(f"[red]error:[/] set ${TOKEN_ENV} to the haku-console Agent bearer")
        raise typer.Exit(2)
    return build_client(url, token)


def _text(result: Any) -> str:
    return "\n".join(block.text for block in (result.content or []) if isinstance(block, mcp_types.TextContent))


@app.command("list")
@async_run
async def list_tools(
    url: str = URL_OPT,
    server: str | None = typer.Option(
        None, "--server", help="Only tools whose name contains this substring (e.g. an upstream server id)."
    ),
) -> None:
    """List available tools as ``name<TAB>first line of description``."""
    async with _client(url) as client:
        tools = await client.list_tools()
    for tool in sorted(tools, key=lambda t: t.name):
        if server and server not in tool.name:
            continue
        summary = (tool.description or "").strip().splitlines()
        _out.print(f"{tool.name}\t{summary[0]}" if summary else tool.name, highlight=False)


@app.command("schema")
@async_run
async def schema(tool: str = typer.Argument(..., help="Tool name."), url: str = URL_OPT) -> None:
    """Print a tool's input JSON schema (handy for building ``call`` arguments)."""
    async with _client(url) as client:
        tools = await client.list_tools()
    match = next((t for t in tools if t.name == tool), None)
    if match is None:
        _err.print(f"[red]error:[/] no tool named {tool!r}")
        raise typer.Exit(1)
    _out.print_json(json.dumps(match.inputSchema, default=str))


@app.command("call")
@async_run
async def call_tool(
    tool: str = typer.Argument(..., help="Tool name."),
    arguments: str = typer.Argument("{}", help="JSON object of arguments."),
    url: str = URL_OPT,
    raw: bool = typer.Option(False, "--json", help="Print the raw JSON result instead of a rendered one."),
) -> None:
    """Call a tool with JSON arguments and print its result."""
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise typer.BadParameter("arguments must be a JSON object")
    async with _client(url) as client:
        result = await client.call_tool(tool, parsed, raise_on_error=False)

    if result.structured_content is not None:
        rendered = json.dumps(result.structured_content, indent=2, default=str)
        _out.print(rendered, highlight=False) if raw else _out.print_json(rendered)
    else:
        _out.print(_text(result), highlight=False)

    if result.is_error:
        raise typer.Exit(1)


main = get_command(app)

if __name__ == "__main__":
    main()
