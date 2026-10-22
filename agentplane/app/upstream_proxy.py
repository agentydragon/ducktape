"""Operator calls to a service's generated HTTP/JSON facade, through the app.

The browser keeps one origin and one credential, its operator session. A GET under
`/upstream/dispatcher/` is forwarded to the Dispatcher with its projected ServiceAccount token.
Calls under `/upstream/actions/` use the request-bound operator token and the Actions facade's
versioned HTTP methods. The service's status and body come back as they are, including a stream.
Browser credentials are never forwarded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import anyio
import httpx

# Establishing a connection is bounded; a response, like a watch, is not.
_TIMEOUT = httpx.Timeout(None, connect=5)


class UpstreamProxy:
    def __init__(self, client: httpx.AsyncClient, token_file: Path | None) -> None:
        self._client = client
        self._token_file = token_file

    async def open_get(self, path: str, query: str) -> httpx.Response:
        """The upstream's response to `GET /{path}?{query}`, its body not yet read. The token is read on
        every call because it rotates."""
        if self._token_file is None:
            raise RuntimeError("this upstream proxy has no projected token file")
        return await self.open_request("GET", path, query, bearer=self._token_file.read_text().strip(), headers={})

    async def open_request(
        self,
        method: str,
        path: str,
        query: str,
        *,
        bearer: str | None,
        headers: Mapping[str, str],
        content: bytes | None = None,
    ) -> httpx.Response:
        """The upstream's response to an explicitly selected method and path, its body unread.

        Only representation headers cross the proxy boundary; browser cookies and credentials never do.
        """
        forwarded = {key: value for key, value in headers.items() if key.lower() in {"accept", "content-type"}}
        user_agent = next((value for key, value in headers.items() if key.lower() == "user-agent"), None)
        if user_agent is not None:
            forwarded["User-Agent"] = user_agent[:300]
        if bearer is not None:
            forwarded["Authorization"] = f"Bearer {bearer}"
        request = self._client.build_request(
            method,
            httpx.URL(path=f"/{path}", query=query.encode()),
            headers={**forwarded, "Accept-Encoding": "identity"},
            content=content,
            timeout=_TIMEOUT,
        )
        return await self._client.send(request, stream=True, follow_redirects=False)


async def response_body(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """`upstream`'s body as it arrives, then closed, however the stream ends."""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        # A disconnect can interrupt the stream at a yield, under a cancelled scope.
        with anyio.CancelScope(shield=True):
            await upstream.aclose()
