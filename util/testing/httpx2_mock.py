"""A minimal respx-style request router for httpx2 clients.

httpx2 is a separate fork of httpx (see mcp_infra/authentik_auth/fastmcp_proxy.py's
module docstring for why fastmcp depends on it), and respx patches httpx's own transport
internals -- it does not see traffic from an httpx2 client at all. This router gives
tests of httpx2-based clients (fastmcp/mcp-sdk internals, or code that constructs a
client fastmcp binds to, e.g. OpenAPIProvider) the same method+path routing,
sequenced responses, and call-count assertions respx provides for httpx, via an
explicitly injected httpx2.MockTransport instead of respx's global patching.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx2


@dataclass
class MockCall:
    """One request a route actually received."""

    request: httpx2.Request


@dataclass
class MockRoute:
    """A registered (method, path)'s response outcomes and received calls."""

    _outcomes: list[httpx2.Response | Exception]
    calls: list[MockCall] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _next_outcome(self) -> httpx2.Response | Exception:
        # Once outcomes are exhausted, repeat the last one -- matches respx's
        # side_effect behavior for a request count beyond the configured sequence.
        # `calls` already includes the call this outcome answers (the caller appends
        # before calling this), so the current call's 0-indexed position is len - 1.
        index = min(len(self.calls) - 1, len(self._outcomes) - 1)
        return self._outcomes[index]


class _RouteBuilder:
    def __init__(self, router: MockRouter, method: str, path: str) -> None:
        self._router = router
        self._method = method
        self._path = path

    def respond(self, *, status_code: int = 200, json: object = None, text: str | None = None) -> MockRoute:
        response = (
            httpx2.Response(status_code, text=text) if text is not None else httpx2.Response(status_code, json=json)
        )
        return self.mock(side_effect=[response])

    def mock(
        self,
        *,
        side_effect: Sequence[httpx2.Response | Exception] | None = None,
        return_value: httpx2.Response | None = None,
    ) -> MockRoute:
        if (side_effect is None) == (return_value is None):
            raise ValueError("mock() takes exactly one of side_effect or return_value")
        outcomes: list[httpx2.Response | Exception]
        if side_effect is not None:
            outcomes = list(side_effect)
        else:
            assert return_value is not None
            outcomes = [return_value]
        route = MockRoute(_outcomes=outcomes)
        self._router._routes[(self._method, self._path)] = route
        return route


class MockRouter:
    """Routes httpx2 requests by (method, path) to a fixed or sequenced response.

    Usage:
        router = MockRouter()
        router.get("/objects/products").respond(json=PRODUCTS)
        post_route = router.post("/stock/products/1/add").mock(
            side_effect=[httpx2.Response(500, json={}), httpx2.Response(200, json=ADD_RESPONSE)]
        )
        async with SomeHttpx2Client(base_url=..., transport=router.transport()) as client:
            ...
        assert post_route.call_count == 2
    """

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], MockRoute] = {}

    def get(self, path: str) -> _RouteBuilder:
        return _RouteBuilder(self, "GET", path)

    def post(self, path: str) -> _RouteBuilder:
        return _RouteBuilder(self, "POST", path)

    def put(self, path: str) -> _RouteBuilder:
        return _RouteBuilder(self, "PUT", path)

    def delete(self, path: str) -> _RouteBuilder:
        return _RouteBuilder(self, "DELETE", path)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        key = (request.method, request.url.path)
        route = self._routes.get(key)
        if route is None:
            raise AssertionError(f"MockRouter: no route registered for {request.method} {request.url.path}")
        route.calls.append(MockCall(request=request))
        outcome = route._next_outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)
