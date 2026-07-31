"""Reject CONNECT/requests whose host is not on the allowlist."""

import re

from mitmproxy import http

ALLOWED = [
    r"^github\.com$",
    r"^api\.github\.com$",
    r"^codeload\.github\.com$",
    r"^objects\.githubusercontent\.com$",
    r"^raw\.githubusercontent\.com$",
]
PATTERNS = [re.compile(p) for p in ALLOWED]


def _allowed(host: str) -> bool:
    return any(p.match(host) for p in PATTERNS)


def http_connect(flow: http.HTTPFlow) -> None:
    if not _allowed(flow.request.host):
        flow.response = http.Response.make(403, b"blocked by lab allowlist\n", {"Content-Type": "text/plain"})


def request(flow: http.HTTPFlow) -> None:
    if not _allowed(flow.request.host):
        flow.response = http.Response.make(403, b"blocked by lab allowlist\n", {"Content-Type": "text/plain"})
