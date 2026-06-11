from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from mcp_infra.enhanced.flat_mixin import FlatTool
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.read_image import ReadImageInput, validate_and_encode_image
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec

logger = logging.getLogger(__name__)


class DirectExecServer(EnhancedFastMCP):
    """Direct (unsandboxed) exec MCP server with typed tool access."""

    # Tool references (assigned in __init__)
    exec_tool: FlatTool

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

        async def exec(input: DirectExecArgs) -> BaseExecResult:
            """Execute a command locally (no sandbox)."""
            return await run_direct_exec(input, default_cwd=default_cwd_val)

        self.exec_tool = self.flat_model()(exec)

        def read_image(input: ReadImageInput) -> list[mcp_types.ImageContent]:
            """Read an image file and return it for the model to see."""
            p = Path(input.path)
            if not p.is_file():
                raise ValueError(f"Not a file: {input.path}")
            return [validate_and_encode_image(p.read_bytes(), input.path)]

        self.read_image_tool = self.flat_model()(read_image)


def main() -> None:
    """Standalone entry point for DirectExecServer over streamable-http.

    This is the privileged exec backend for the approval gate. Run in a container
    with the required secrets/credentials mounted. The approval gate connects to
    this server over HTTP and forwards approved tool calls.

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
