"""Typed stubs for exec MCP servers."""

from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs
from mcp_infra.stubs.server_stubs import ServerStub


class DirectExecServerStub(ServerStub):
    """Typed stub for direct (unsandboxed) exec server operations."""

    async def exec(self, input: DirectExecArgs) -> BaseExecResult:
        raise NotImplementedError  # Auto-wired at runtime
