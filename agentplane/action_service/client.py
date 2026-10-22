"""Client for the Action Service's operator/BFF API surface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID

import httpx
from anyio import CancelScope
from pydantic import BaseModel, TypeAdapter

from agentplane.action_service.connections import Connection, ConnectionRebind, ConnectionRename, ConnectionVersion
from agentplane.action_service.enrollments import (
    EnrollmentDecisionInput,
    EnrollmentDecisionResult,
    EnrollmentPreview,
    EnrollmentPreviewInput,
)
from agentplane.action_service.mcp_linkage import McpLinkageStart, McpLinkageStartView, McpLinkageView
from agentplane.subjects import ServiceAccountRef


class OperatorActionServiceClient:
    """BFF-facing client; its authenticator and paths are distinct from Sandbox workload auth."""

    def __init__(self, http: httpx.AsyncClient, token: Callable[[], Awaitable[str]]) -> None:
        """`token` is awaited before each request, for the bearer that request carries."""
        self._http = http
        self._token = token

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = await self._token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        response = await self._http.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    async def _call[T](
        self, adapter: TypeAdapter[T], method: str, url: str, *, body: BaseModel | None = None, **kwargs
    ) -> T:
        json = body.model_dump(mode="json") if body is not None else None
        return adapter.validate_json((await self._request(method, url, json=json, **kwargs)).content)

    async def complete_mcp_linkage(self, state: str, code: str) -> McpLinkageView:
        params = {"state": state, "code": code}
        return await self._call(TypeAdapter(McpLinkageView), "GET", "/v1/mcp-linkage/callback", params=params)

    async def start_mcp_linkage(self, server_id: str, scopes: list[str]) -> McpLinkageStartView:
        url = f"/v1/operator/mcp-servers/{server_id}/linkage/start"
        return await self._call(TypeAdapter(McpLinkageStartView), "POST", url, body=McpLinkageStart(scopes=scopes))

    async def disconnect_mcp_linkage(self, server_id: str) -> McpLinkageView:
        url = f"/v1/operator/mcp-servers/{server_id}/linkage/disconnect"
        return await self._call(TypeAdapter(McpLinkageView), "POST", url)

    async def caller_service_accounts(self) -> list[ServiceAccountRef]:
        return await self._call(TypeAdapter(list[ServiceAccountRef]), "GET", "/v1/operator/caller-service-accounts")

    async def preview_enrollment(self, handle: str, body: EnrollmentPreviewInput) -> EnrollmentPreview:
        url = f"/v1/operator/connection-enrollments/{handle}/preview"
        return await self._call(TypeAdapter(EnrollmentPreview), "POST", url, body=body)

    async def decide_enrollment(self, handle: str, body: EnrollmentDecisionInput) -> EnrollmentDecisionResult:
        url = f"/v1/operator/connection-enrollments/{handle}/decision"
        return await self._call(TypeAdapter(EnrollmentDecisionResult), "POST", url, body=body)

    async def connections(self) -> list[Connection]:
        return await self._call(TypeAdapter(list[Connection]), "GET", "/v1/operator/connections")

    async def connection(self, connection_id: UUID) -> Connection:
        return await self._call(TypeAdapter(Connection), "GET", f"/v1/operator/connections/{connection_id}")

    async def rename_connection(self, connection_id: UUID, body: ConnectionRename) -> Connection:
        url = f"/v1/operator/connections/{connection_id}"
        return await self._call(TypeAdapter(Connection), "PATCH", url, body=body)

    async def rebind_connection(self, connection_id: UUID, body: ConnectionRebind) -> Connection:
        url = f"/v1/operator/connections/{connection_id}/rebind"
        return await self._call(TypeAdapter(Connection), "POST", url, body=body)

    async def unbind_connection(self, connection_id: UUID, body: ConnectionVersion) -> Connection:
        url = f"/v1/operator/connections/{connection_id}/unbind"
        return await self._call(TypeAdapter(Connection), "POST", url, body=body)

    @asynccontextmanager
    async def _stream_operator(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Operator SSE open/check/close, with a fresh exchanged token for each open."""
        token = await self._token()
        request = self._http.build_request("GET", path, params=params, headers={"Authorization": f"Bearer {token}"})
        response = await self._http.send(request, stream=True)
        try:
            response.raise_for_status()
            yield response.aiter_raw()
        finally:
            with CancelScope(shield=True):
                await response.aclose()

    def stream_connections(self) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        return self._stream_operator("/v1/operator/connections/stream")

    def stream_mcp_linkages(self) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        return self._stream_operator("/v1/operator/mcp-servers/stream")
