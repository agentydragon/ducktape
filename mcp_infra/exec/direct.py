from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import mcp.types as mcp_types
from fastmcp.server import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import Field

from mcp_infra.exec.read_image import validate_and_encode_image
from util.exec.models import CMD_DESCRIPTION, BaseExecResult, TimeoutMs
from util.exec.subprocess import DirectExecArgs, run_direct_exec

logger = logging.getLogger(__name__)


class DirectExecServer(FastMCP):
    """Direct (unsandboxed) exec MCP server with typed tool access."""

    # Tool references (assigned in __init__)
    exec_tool: Callable[..., Awaitable[BaseExecResult]]

    def __init__(
        self,
        *,
        default_cwd: Path | None = None,
        instructions: str = "Local command execution (unsandboxed)",
        auth: AuthProvider | None = None,
    ):
        kwargs: dict[str, Any] = {}
        if auth is not None:
            kwargs["auth"] = auth
        super().__init__("Direct Exec MCP Server", instructions=instructions, **kwargs)

        # Capture default_cwd in closure
        default_cwd_val = default_cwd

        async def exec(
            *,
            cmd: Annotated[list[str], Field(min_length=1, description=CMD_DESCRIPTION)],
            max_bytes: Annotated[
                int,
                Field(ge=0, le=100_000, description="Max bytes to capture from stdout and stderr. 0 means both empty."),
            ],
            # Required but nullable so strict tool calls send null when no cwd override is requested.
            cwd: str | None,
            timeout_ms: TimeoutMs,
        ) -> BaseExecResult:
            """Execute a command locally (no sandbox)."""
            args = DirectExecArgs(cmd=cmd, max_bytes=max_bytes, cwd=cwd, timeout_ms=timeout_ms)
            return await run_direct_exec(args, default_cwd=default_cwd_val)

        self.exec_tool = self.tool(output_schema=None)(exec)

        def read_image(path: str) -> list[mcp_types.ImageContent]:
            """Read an image file and return it for the model to see."""
            p = Path(path)
            if not p.is_file():
                raise ValueError(f"Not a file: {path}")
            return [validate_and_encode_image(p.read_bytes(), path)]

        self.read_image_tool = self.tool(output_schema=None)(read_image)


def main() -> None:
    """Standalone entry point for DirectExecServer over streamable-http.

    Standalone privileged exec backend. Run in a container with the required
    secrets/credentials mounted, behind an authorization boundary appropriate
    for its caller.

    Environment variables:
      TOKEN        — if set, require this bearer token on incoming requests
      INSTRUCTIONS — if set, override the default MCP server instructions
    """
    parser = argparse.ArgumentParser(description="DirectExecServer — streamable-http backend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--cwd", default=None, help="Default working directory for commands")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    default_cwd = Path(args.cwd) if args.cwd else None
    token = os.environ.get("TOKEN")
    auth = StaticTokenVerifier(tokens={token: {"client_id": "caller"}}) if token else None
    instructions = os.environ.get("INSTRUCTIONS", "Local command execution (unsandboxed)")
    server = DirectExecServer(default_cwd=default_cwd, instructions=instructions, auth=auth)
    logger.info("starting DirectExecServer on %s:%d", args.host, args.port)
    server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
