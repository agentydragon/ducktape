"""Operator reads of a service's generated HTTP/JSON facade, through the app.

The browser keeps one origin and one credential, its operator session. A GET under
`/upstream/<service>/` is forwarded to that service's facade with the app's projected
ServiceAccount token, and the service's status and body come back as they are, a stream included.
Nothing is read from the request body and no route is listed here: what the browser can reach is the
set of `GET` rules the service's proto annotates, and its own authentication runs on every call.
Commands are not reachable this way; they go through the app's typed routes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import httpx

# Establishing a connection is bounded; a response, like a watch, is not.
_TIMEOUT = httpx.Timeout(None, connect=5)


class UpstreamProxy:
    def __init__(self, client: httpx.AsyncClient, token_file: Path) -> None:
        self._client = client
        self._token_file = token_file

    async def open_get(self, path: str, query: str) -> httpx.Response:
        """The upstream's response to `GET /{path}?{query}`, its body not yet read. The token is read on
        every call because it rotates."""
        request = self._client.build_request(
            "GET",
            httpx.URL(path=f"/{path}", query=query.encode()),
            headers={"Authorization": f"Bearer {self._token_file.read_text().strip()}", "Accept-Encoding": "identity"},
            timeout=_TIMEOUT,
        )
        return await self._client.send(request, stream=True)


async def response_body(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """`upstream`'s body as it arrives, then closed, however the stream ends."""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        # A disconnect can interrupt the stream at a yield, under a cancelled scope.
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
