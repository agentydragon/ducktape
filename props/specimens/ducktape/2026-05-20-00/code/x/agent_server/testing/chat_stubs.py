"""Typed stubs for chat MCP servers."""

from mcp_infra.stubs.server_stubs import ServerStub
from x.agent_server.mcp.chat.server import PostInput, PostResult, ReadPendingInput, ReadPendingResult


class ChatServerStub(ServerStub):
    """Typed stub for chat server operations."""

    async def post(self, input: PostInput) -> PostResult:
        raise NotImplementedError  # Auto-wired at runtime

    async def read_pending_messages(self, input: ReadPendingInput) -> ReadPendingResult:
        raise NotImplementedError  # Auto-wired at runtime
