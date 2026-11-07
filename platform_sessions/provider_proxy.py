"""Private HTTP data plane for one or more provider-owned sessions."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from platform_sessions.auth import BearerAuth
from platform_sessions.proxy import _REQUEST_PRIVATE, _RESPONSE_PRIVATE, ProxyInstance, ProxyRequest, _filter_headers
from platform_sessions.routes import authorize_route

RESULT_HEADER = b"x-platform-session-proxy-result"
RESULT_VERSION = b"1"


class ProviderProxy:
    """Accept only authenticated gateway requests and dispatch once locally.

    Instances and upstream origins are fixed in provider configuration. Gateway
    owner and route credentials are distinct principals; callers cannot supply
    an owner flag in a header. Responses carry a private protocol marker only
    after dispatch, allowing the gateway to distinguish an upstream 401 from a
    rejection by this endpoint.
    """

    def __init__(self, instances: Mapping[str, ProxyInstance], auth: BearerAuth):
        if any(key != instance.config.id for key, instance in instances.items()):
            raise ValueError("Proxy instance ID mismatch")
        self.instances = instances
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive=receive)
        response = await self._request(request)
        await response(scope, receive, send)

    async def _request(self, request: Request) -> Response:
        principal = self.auth.authenticate(request)
        if principal is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        parts = request.scope.get("raw_path", b"").split(b"/", 4)
        if len(parts) != 5 or parts[1:3] != [b"internal", b"proxy"]:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            instance_id = parts[3].decode("ascii")
            path = (b"/" + parts[4]).decode("ascii")
        except UnicodeError:
            return JSONResponse({"error": "invalid_path"}, status_code=400)
        instance = self.instances.get(instance_id)
        if instance is None or not instance.config.enabled:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            mapped = authorize_route(
                request.method,
                path,
                instance.config.upstream_base_url,
                None if principal.owner else principal.routes.get(instance_id, ()),
            )
        except ValueError:
            return JSONResponse({"error": "invalid_path"}, status_code=400)
        if mapped is None:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            result = await instance.transport.request(
                ProxyRequest(
                    method=request.method,
                    url=mapped.upstream_url,
                    query=request.scope.get("query_string", b""),
                    headers=_filter_headers(
                        request.scope["headers"], _REQUEST_PRIVATE | {RESULT_HEADER}, instance.headers.request
                    ),
                    body=await request.body(),
                    owner=principal.owner,
                )
            )
            headers = _filter_headers(
                result.headers, _RESPONSE_PRIVATE | {b"content-length", RESULT_HEADER}, instance.headers.response
            )
            response = Response(content=result.body, status_code=result.status)
            response.raw_headers.extend(headers)
            response.raw_headers.append((RESULT_HEADER, RESULT_VERSION))
            return response
        except Exception:
            # Exception messages may contain credentials or member data.
            return JSONResponse({"error": "upstream_unavailable"}, status_code=502)
