"""Typed stubs for exec MCP servers."""

from mcp_infra.stubs.server_stubs import ServerStub
from util.exec.models import BaseExecResult
from util.exec.subprocess import DirectExecArgs


class DirectExecServerStub(ServerStub):
    """Typed stub for direct (unsandboxed) exec server operations."""

    async def exec(self, input: DirectExecArgs) -> BaseExecResult:
        raise NotImplementedError  # Auto-wired at runtime
