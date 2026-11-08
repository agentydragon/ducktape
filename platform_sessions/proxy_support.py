"""HTTP transport for provider-owned sessions."""

from collections.abc import Callable

import httpx2
from starlette.concurrency import run_in_threadpool

from platform_sessions.proxy import ProxyResponse


class HttpSessionTransport:
    def __init__(self, store, session_headers: Callable, transport=None):
        self.store = store
        self.session_headers = session_headers
        self.transport = transport

    async def request(self, request):
        return await run_in_threadpool(self._request, request)

    def _request(self, request):
        session = self.store.load()
        if session is None:
            return ProxyResponse(503, (), b"Session unavailable")
        headers = list(request.headers) + list(self.session_headers(session))
        url = request.url + ("?" + request.query.decode("ascii") if request.query else "")
        with (
            httpx2.Client(transport=self.transport, trust_env=True, follow_redirects=False, timeout=20) as client,
            client.stream(request.method, url, headers=headers, content=request.body) as response,
        ):
            # Preserve raw compressed bytes alongside the upstream encoding header.
            body = b"".join(response.iter_raw())
            return ProxyResponse(response.status_code, tuple(response.headers.raw), body)
