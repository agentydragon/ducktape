from __future__ import annotations

from pathlib import Path

import mcp.types as mcp_types

from mcp_infra.enhanced.flat_mixin import FlatTool
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.read_image import ReadImageInput, validate_and_encode_image
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec


class DirectExecServer(EnhancedFastMCP):
    """Direct (unsandboxed) exec MCP server with typed tool access."""

    # Tool references (assigned in __init__)
    exec_tool: FlatTool

    def __init__(self, *, default_cwd: Path | None = None):
        """Create a direct (unsandboxed) exec MCP server.

        Args:
            default_cwd: Default working directory for commands when not specified
        """
        super().__init__("Direct Exec MCP Server", instructions="Local command execution (unsandboxed)")

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
