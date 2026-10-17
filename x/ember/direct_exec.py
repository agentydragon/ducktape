from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import mcp.types as mcp_types
from fastmcp.server import FastMCP
from pydantic import Field

from mcp_infra.exec.read_image import validate_and_encode_image
from util.exec.models import CMD_DESCRIPTION, BaseExecResult, TimeoutMs
from util.exec.subprocess import DirectExecArgs, run_direct_exec


class DirectExecServer(FastMCP):
    """Ember's direct (unsandboxed) exec MCP server with typed tool access."""

    # Tool references (assigned in __init__)
    exec_tool: Callable[..., Awaitable[BaseExecResult]]

    def __init__(self, *, default_cwd: Path | None = None):
        super().__init__("Ember Exec MCP Server", instructions="Local command execution (unsandboxed)")

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
