"""Authenticated, allowlisted proxy for integration-owned sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from platform_sessions.auth import BearerAuth
from platform_sessions.routes import _encoded_path, _segments, authorize_route

# The transport owns upstream authentication and session rotation. These are
# always removed regardless of the additional provider-configured rules.
_HOP = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_REQUEST_PRIVATE = {b"authorization", b"cookie", b"cookie2", b"host"}
_RESPONSE_PRIVATE = {
    b"authorization",
    b"authentication-info",
    b"proxy-authentication-info",
    b"set-cookie",
    b"set-cookie2",
}


@dataclass(frozen=True)
class HeaderPolicy:
    """Headers to strip in addition to hop-by-hop and session headers."""

    request: Set[str] = field(default_factory=set)
    response: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ProxyRequest:
    method: str
    url: str
    query: bytes = field(repr=False)
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    body: bytes = field(repr=False)
    owner: bool = False


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    headers: tuple[tuple[bytes, bytes], ...] = field(repr=False)
    body: bytes = field(repr=False)


class SessionTransport(Protocol):
    """Send once with the owned session; do not retry or follow redirects.

    Forward the raw query unchanged, capture any rotated session state, and
    return response bytes without decoding them.
    """

    async def request(self, request: ProxyRequest) -> ProxyResponse: ...


class ProxyConfig(Protocol):
    """Configured identity and upstream origin used by the proxy."""

    @property
    def id(self) -> str: ...

    @property
    def type(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def upstream_base_url(self) -> str: ...


@dataclass(frozen=True)
class ProxyInstance:
    config: ProxyConfig
    transport: SessionTransport
    headers: HeaderPolicy = field(default_factory=HeaderPolicy)


def _filter_headers(
    headers: Sequence[tuple[bytes, bytes]], mandatory: Set[bytes], configured: Set[str]
) -> tuple[tuple[bytes, bytes], ...]:
    names = {name.lower().encode("ascii") for name in configured}
    for name, value in headers:
        if name.lower() == b"connection":
            names.update(item.strip().lower() for item in value.split(b",") if item.strip())
    blocked = _HOP | mandatory | names
    return tuple((name.lower(), value) for name, value in headers if name.lower() not in blocked)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Invalid redirect")
    port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    return parsed.scheme, parsed.hostname, port


def _relative_location(location: bytes, upstream_url: str, base_url: str) -> bytes:
    try:
        value = location.decode("ascii")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid redirect")
        resolved = urljoin(upstream_url, value)
        target = urlsplit(resolved)
        base = urlsplit(base_url)
        if _origin(resolved) != _origin(base_url):
            raise ValueError("Redirect outside configured origin")
        prefix = _segments(base.path) if base.path else ()
        segments = _segments(target.path or "/")
        if segments[: len(prefix)] != prefix:
            raise ValueError("Redirect outside configured base path")
        suffix = _encoded_path(segments[len(prefix) :])
        return urlunsplit(("", "", suffix, target.query, target.fragment)).encode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Redirect cannot be mapped") from exc


def _rewrite_location(location: bytes, upstream_url: str, base_url: str, instance_id: str) -> bytes:
    return f"/proxy/{instance_id}".encode("ascii") + _relative_location(location, upstream_url, base_url)


class _Proxy:
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
        raw_path = request.scope.get("raw_path", b"")
        parts = raw_path.split(b"/", 3)
        if len(parts) != 4 or parts[1] != b"proxy":
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            instance_id = parts[2].decode("ascii")
            path = b"/" + parts[3]
            raw_path_text = path.decode("ascii")
        except UnicodeError:
            return JSONResponse({"error": "invalid_path"}, status_code=400)
        instance = self.instances.get(instance_id)
        if instance is None or not instance.config.enabled:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            mapped = authorize_route(
                request.method,
                raw_path_text,
                instance.config.upstream_base_url,
                None if principal.owner else principal.routes.get(instance_id, ()),
            )
        except ValueError:
            return JSONResponse({"error": "invalid_path"}, status_code=400)
        if mapped is None:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            # TODO: add streaming when a consumer needs it.
            result = await instance.transport.request(
                ProxyRequest(
                    method=request.method,
                    url=mapped.upstream_url,
                    query=request.scope.get("query_string", b""),
                    headers=_filter_headers(request.scope["headers"], _REQUEST_PRIVATE, instance.headers.request),
                    body=await request.body(),
                    owner=principal.owner,
                )
            )
            headers = _filter_headers(
                result.headers, _RESPONSE_PRIVATE | {b"content-length"}, instance.headers.response
            )
            if 300 <= result.status < 400:
                locations = [value for name, value in headers if name == b"location"]
                if len(locations) > 1:
                    raise ValueError("Multiple redirect locations")
                if locations:
                    location = _rewrite_location(
                        locations[0], mapped.upstream_url, instance.config.upstream_base_url, instance_id
                    )
                    headers = tuple((name, location if name == b"location" else value) for name, value in headers)
            response = Response(content=result.body, status_code=result.status)
            response.raw_headers.extend(headers)
            return response
        except Exception:
            return JSONResponse({"error": "upstream_unavailable"}, status_code=502)


def install_proxy(app: FastAPI, instances: Mapping[str, ProxyInstance], auth: BearerAuth) -> None:
    """Mount the proxy; applications must leave this path to bearer authentication."""
    app.mount("/proxy", _Proxy(instances, auth))
