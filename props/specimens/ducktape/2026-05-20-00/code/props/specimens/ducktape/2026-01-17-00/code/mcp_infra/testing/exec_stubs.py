"""Typed stubs for exec MCP servers."""

from mcp_infra.exec.bwrap import BwrapExecArgs
from mcp_infra.exec.direct import DirectExecArgs
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.seatbelt import SandboxExecArgs, SandboxExecResult
from mcp_infra.stubs.server_stubs import ServerStub


class DirectExecServerStub(ServerStub):
    """Typed stub for direct (unsandboxed) exec server operations."""

    async def exec(self, input: DirectExecArgs) -> BaseExecResult:
        raise NotImplementedError  # Auto-wired at runtime


class BwrapExecServerStub(ServerStub):
    """Typed stub for bubblewrap sandboxed exec server operations."""

    async def exec(self, input: BwrapExecArgs) -> BaseExecResult:
        raise NotImplementedError  # Auto-wired at runtime


class SeatbeltExecServerStub(ServerStub):
    """Typed stub for seatbelt sandboxed exec server operations."""

    async def sandbox_exec(self, input: SandboxExecArgs) -> SandboxExecResult:
        raise NotImplementedError  # Auto-wired at runtime
