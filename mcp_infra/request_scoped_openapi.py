"""Request-scoped HTTP clients for FastMCP OpenAPI tools.

FastMCP 3.4.4 binds one ``httpx.AsyncClient`` to every ``OpenAPITool`` when
the OpenAPI provider is constructed.  Unlike function-backed tools, those
tools have no dependency-injection seam for choosing a client at invocation
time.

``RequestScopedOpenAPIClients`` adapts each generated tool into a transformed
tool whose client comes from FastMCP's call-scoped ``Depends`` resolver.  The
provider may therefore authenticate and yield a fresh client for each tool
invocation.  A shallow Pydantic model copy keeps the generated route/director
metadata while isolating the private client binding from concurrent calls.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from typing import Any, overload

import httpx
from fastmcp.dependencies import Depends
from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.server.providers.openapi import OpenAPITool
from fastmcp.server.transforms import GetToolNext, Transform
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.versions import VersionSpec

# The provider is also used by clients from the httpx2 compatibility fork. The
# OpenAPI adapter relies on FastMCP's runtime-compatible client contract; keeping
# the old httpx subclass bound here rejects those clients statically.
type HTTPClientProvider[ClientT = Any] = Callable[..., AbstractAsyncContextManager[ClientT]]
_INJECTED_CLIENT_PARAMETER = "_fastmcp_request_scoped_http_client"


def borrowed_http_client_provider[ClientT](client: ClientT) -> HTTPClientProvider[ClientT]:
    """Adapt a caller-owned client to the sole provider-based API.

    This is useful for tests and local tooling that already manage a fixed
    client's lifetime.  Production providers instead create and close a fresh
    authenticated client for each tool invocation.
    """

    @asynccontextmanager
    async def provider() -> AsyncIterator[ClientT]:
        yield client

    return provider


class _RequestCompatibleClient:
    """Present FastMCP's ``httpx`` request API to another httpx-compatible client.

    FastMCP 3.4.7's OpenAPI director always constructs an ``httpx.Request``.
    Clients from the ``httpx2`` fork reject that request because its body stream
    is not an ``httpx2`` stream.  Rebuild the already-buffered request through
    the injected client's own builder before sending it; this preserves the
    provider's client, transport, auth, and timeout configuration.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def base_url(self) -> Any:
        return self._client.base_url

    @property
    def headers(self) -> Any:
        return self._client.headers

    async def send(self, request: httpx.Request) -> Any:
        build_request = getattr(self._client, "build_request", None)
        if build_request is None:
            return await self._client.send(request)
        compatible_request = build_request(
            request.method,
            str(request.url),
            headers=request.headers.raw,
            content=request.content,
            extensions=request.extensions,
        )
        return await self._client.send(compatible_request)


class RequestScopedOpenAPIClients(Transform):
    """Resolve an ``AsyncClient`` dependency for each generated tool call."""

    def __init__(self, client_provider: HTTPClientProvider) -> None:
        self._client_provider = client_provider
        # This cache contains only static tool metadata, never request or
        # operator state.  Holding the parent alongside the wrapper protects
        # against a theoretical CPython object-id reuse.
        self._wrapped: dict[int, tuple[OpenAPITool, Tool]] = {}

    @overload
    def _wrap(self, tool: None) -> None: ...

    @overload
    def _wrap(self, tool: Tool) -> Tool: ...

    def _wrap(self, tool: Tool | None) -> Tool | None:
        if tool is None or not isinstance(tool, OpenAPITool):
            return tool
        if _INJECTED_CLIENT_PARAMETER in tool.parameters.get("properties", {}):
            raise ValueError(f"OpenAPI tool {tool.name!r} uses reserved parameter {_INJECTED_CLIENT_PARAMETER!r}")

        cached = self._wrapped.get(id(tool))
        if cached is not None and cached[0] is tool:
            return cached[1]

        client_provider = self._client_provider
        injected_client = Depends(client_provider)

        async def dispatch(
            _fastmcp_request_scoped_http_client: httpx.AsyncClient = injected_client, **arguments: Any
        ) -> ToolResult:
            # OpenAPITool has no public per-call client factory in FastMCP
            # 3.4.4.  model_copy() preserves its generated route/director while
            # ensuring this private client assignment is invocation-local.
            bound = tool.model_copy()
            bound._client = _RequestCompatibleClient(_fastmcp_request_scoped_http_client)
            return await bound.run(arguments)

        wrapped = Tool.from_tool(tool, transform_fn=without_injected_parameters(dispatch))
        # Tool.from_tool derives a strict schema from the forwarding callable.
        # The generated OpenAPI schema is already the contract and must remain
        # byte-for-byte equivalent (including additionalProperties semantics).
        wrapped.parameters = deepcopy(tool.parameters)

        self._wrapped[id(tool)] = (tool, wrapped)
        return wrapped

    async def list_tools(self, tools: Sequence[Tool]) -> Sequence[Tool]:
        """Wrap generated tools while leaving other provider tools unchanged."""
        return [self._wrap(tool) for tool in tools]

    async def get_tool(self, name: str, call_next: GetToolNext, *, version: VersionSpec | None = None) -> Tool | None:
        """Return the same cached wrapper used by ``list_tools``."""
        return self._wrap(await call_next(name, version=version))
