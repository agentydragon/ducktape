"""A FastMCP client speaking streamable HTTP to an in-process ASGI app, and reading its text results."""

from __future__ import annotations

from collections.abc import Callable

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import ContentBlock, TextContent
from more_itertools import one


def asgi_mcp_client(transport: Callable[[], httpx2.AsyncBaseTransport], bearer: str) -> Client[StreamableHttpTransport]:
    """`transport` is called once per HTTP client the MCP session opens."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        *,
        follow_redirects: bool = True,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=transport(),
            headers=headers,
            timeout=timeout or httpx2.Timeout(60),
            auth=auth,
            follow_redirects=follow_redirects,
        )

    return Client(
        StreamableHttpTransport(
            "http://actions.test/mcp", headers={"Authorization": f"Bearer {bearer}"}, httpx_client_factory=factory
        )
    )


def text_of(content: list[ContentBlock]) -> str:
    return one(block.text for block in content if isinstance(block, TextContent))
