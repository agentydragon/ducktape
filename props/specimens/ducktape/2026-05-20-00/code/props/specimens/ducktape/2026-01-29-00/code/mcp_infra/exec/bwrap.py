from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import which

import mcp.types as mcp_types
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.enhanced.flat_mixin import FlatTool
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.exec.models import BaseExecResult, ExecOutcome, TimeoutMs, render_outcome_to_result
from mcp_infra.exec.read_image import ReadImageInput, validate_and_encode_image
from mcp_infra.exec.subprocess import run_proc

BWRAP = os.getenv("BWRAP", "bwrap")
ALLOW_UNSHARE_NET = os.getenv("DUCK_UNSHARE_NET", "0") == "1"


async def _run_in_bwrap(cmd: list[str], timeout_s: float, cwd: Path | None, stdin_text: str | None) -> ExecOutcome:
    if sys.platform != "linux":
        raise ToolError("NOT_LINUX: bubblewrap sandbox available only on Linux")
    if which(BWRAP) is None:
        raise ToolError("BWRAP_NOT_FOUND: bubblewrap (bwrap) not found in PATH")

    cwd_val = str(cwd or Path.cwd())

    argv: list[str] = [BWRAP, "--unshare-all", "--die-with-parent"]
    if ALLOW_UNSHARE_NET:
        argv.append("--unshare-net")

    argv += [
        "--ro-bind",
        "/",
        "/",
        "--bind",
        cwd_val,
        cwd_val,
        "--chdir",
        cwd_val,
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--setenv",
        "HOME",
        "/tmp",
        "--",
        *cmd,
    ]

    # chdir handled inside bwrap; pass cwd=None to subprocess
    return await run_proc(argv, timeout_s=timeout_s, cwd=None, stdin=stdin_text)


class BwrapExecArgs(BaseModel):
    cmd: list[str]
    max_bytes: int = Field(..., ge=0, le=100_000, description="Applies to stdin and captures")
    # str not Path: OpenAI strict mode doesn't accept format="path" in JSON schemas
    cwd: str | None = None
    timeout_ms: TimeoutMs
    stdin_text: str | None = None

    model_config = ConfigDict(extra="forbid")


class BwrapExecServer(EnhancedFastMCP):
    """Bubblewrap-sandboxed exec MCP server with typed tool access (Linux only)."""

    # Tool references (assigned in __init__)
    exec_tool: FlatTool

    def __init__(self, *, default_cwd: Path | None = None):
        """Create a bubblewrap-sandboxed exec MCP server (Linux only).

        Args:
            default_cwd: Default working directory for commands when not specified
        """
        super().__init__("Bubblewrap Exec MCP Server", instructions="Local command execution via bubblewrap (Linux)")

        # Capture default_cwd in closure
        default_cwd_val = default_cwd

        async def exec(input: BwrapExecArgs) -> BaseExecResult:
            """Execute a command inside a bubblewrap sandbox (Linux only)."""
            if not input.cmd or not all(isinstance(x, str) for x in input.cmd):
                raise ToolError("INVALID_CMD: cmd must be a non-empty list[str]")
            cwd_val: Path | None = Path(input.cwd) if input.cwd else None
            if cwd_val is None and default_cwd_val is not None:
                cwd_val = default_cwd_val

            timeout_s = max(0.001, input.timeout_ms / 1000.0)
            outcome = await _run_in_bwrap(input.cmd, timeout_s, cwd_val, input.stdin_text)
            return render_outcome_to_result(outcome, input.max_bytes)

        self.exec_tool = self.flat_model()(exec)

        def read_image(input: ReadImageInput) -> list[mcp_types.ImageContent]:
            """Read an image file and return it for the model to see."""
            # TODO: should respect bwrap sandbox boundaries
            p = Path(input.path)
            if not p.is_file():
                raise ValueError(f"Not a file: {input.path}")
            return [validate_and_encode_image(p.read_bytes(), input.path)]

        self.read_image_tool = self.flat_model()(read_image)
