"""Authenticated, allowlisted proxy for integration-owned sessions."""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

from platform_sessions.routes import _encoded_path, _segments

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
