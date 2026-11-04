"""Route allowlists and safe paths for a session-aware upstream proxy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urlsplit, urlunsplit

from platform_sessions.registry import _upstream_url

_BAD_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_METHOD = re.compile(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]*\Z")
_SEGMENT_SAFE = "-._~!$&'()*+,;=:@"


def _segments(path: str, *, pattern: bool = False) -> tuple[str, ...]:
    """Parse once, rejecting paths with ambiguous segment boundaries."""
    if not path.startswith("/") or not path.isascii() or _BAD_PERCENT.search(path):
        raise ValueError("Invalid route path")
    if any(ord(c) < 32 or ord(c) == 127 for c in path) or any(c in path for c in "\\?#"):
        raise ValueError("Invalid route path")
    if path == "/":
        return ()
    segments = []
    parts = path[1:].split("/")
    for index, raw in enumerate(parts):
        if not raw and index != len(parts) - 1:
            raise ValueError("Empty path segment")
        try:
            value = unquote_to_bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid UTF-8 in route path") from exc
        if value in {".", ".."} or any(c in value for c in "/\\%") or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Ambiguous route path")
        if pattern and "*" in value and (value != "*" or raw != "*"):
            raise ValueError("A wildcard must occupy one complete segment")
        segments.append(value)
    return tuple(segments)


def _encoded_path(segments: tuple[str, ...]) -> str:
    return "/" + "/".join(quote(segment, safe=_SEGMENT_SAFE) for segment in segments)


@dataclass(frozen=True)
class Route:
    """A method and a path with optional single-segment wildcards."""

    method: str
    path: str

    def __post_init__(self) -> None:
        if not _METHOD.fullmatch(self.method):
            raise ValueError("Invalid route method")
        _segments(self.path, pattern=True)

    def matches(self, method: str, segments: tuple[str, ...]) -> bool:
        pattern = _segments(self.path, pattern=True)
        return (
            method == self.method
            and len(pattern) == len(segments)
            and all(
                (expected == "*" and bool(actual)) or expected == actual
                for expected, actual in zip(pattern, segments, strict=True)
            )
        )


@dataclass(frozen=True)
class MappedRoute:
    """The authorized relative path and its resolved upstream URL."""

    relative_path: str
    upstream_url: str


def authorize_route(method: str, raw_path: str, base_url: str, routes: tuple[Route, ...] | None) -> MappedRoute | None:
    """Authorize the request path and map that same interpretation upstream.

    The caller supplies the raw URL path without query; query bytes are handled
    separately and do not participate in route matching.
    """
    segments = _segments(raw_path)
    if routes is not None and not any(route.matches(method, segments) for route in routes):
        return None
    parsed = urlsplit(_upstream_url(base_url))
    prefix = _segments(parsed.path) if parsed.path else ()
    upstream_path = _encoded_path(prefix + segments)
    return MappedRoute(
        relative_path=_encoded_path(segments),
        upstream_url=urlunsplit((parsed.scheme, parsed.netloc, upstream_path, "", "")),
    )
