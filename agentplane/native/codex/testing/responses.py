"""Typed fixture endpoint for the OpenAI Responses wire API."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from agentplane.native.codex.testing.requests import ResponsesRequest
from agentplane.native.testing.model_endpoint import ModelEndpoint, ModelExchange


@dataclass(frozen=True)
class _Catalog:
    body: bytes
    etag: str


class OpenAIResponses(ModelEndpoint[ResponsesRequest]):
    """Scripted OpenAI Responses endpoint; no HTTP request objects escape it.

    Like LLM ingress, it answers `/agentplane/codex/models` with the catalog given to `serve_catalog` (404 until
    then) and stamps that catalog's `ETag` on every Responses reply as `X-Models-Etag`.
    """

    def __init__(self) -> None:
        super().__init__()
        self._catalog: _Catalog | None = None
        self._catalog_status = 200
        self.catalog_authorizations: list[str | None] = []

    def serve_catalog(self, models: Sequence[Mapping[str, Any]], *, status: int = 200) -> None:
        """Replace the served catalog; with a `status` other than 200, catalog requests fail with it."""
        body = json.dumps({"models": list(models)}).encode()
        self._catalog = _Catalog(body=body, etag=f'"{hashlib.sha256(body).hexdigest()[:16]}"')
        self._catalog_status = status

    def _add_routes(self, app: web.Application) -> None:
        super()._add_routes(app)
        app.router.add_get("/agentplane/codex/models", self._catalog_response)

    def _response_headers(self) -> Mapping[str, str]:
        return {} if self._catalog is None else {"X-Models-Etag": self._catalog.etag}

    async def _catalog_response(self, request: web.Request) -> web.Response:
        self.catalog_authorizations.append(request.headers.get("Authorization"))
        if self._catalog is None:
            return web.json_response({"error": "no Codex model catalog is configured"}, status=404)
        if self._catalog_status != 200:
            return web.json_response({"error": "catalog unavailable"}, status=self._catalog_status)
        return web.Response(
            body=self._catalog.body, content_type="application/json", headers={"ETag": self._catalog.etag}
        )

    def _parse_request(self, value: Any) -> ResponsesRequest:
        return ResponsesRequest.model_validate(value)

    async def __aenter__(self) -> OpenAIResponses:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def await_next_request(self) -> ModelExchange[ResponsesRequest]:
        return await self._next_exchange()
