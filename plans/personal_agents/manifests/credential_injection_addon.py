"""Domain allowlist + GitHub credential injection.

The agent holds no GitHub credential at all. Requests leave it unauthenticated;
this addon attaches the real token on the way out, and only for GitHub hosts.
"""

import logging
import os
import re

from mitmproxy import http

logger = logging.getLogger(__name__)

ALLOWED = [
    r"^github\.com$",
    r"^api\.github\.com$",
    r"^codeload\.github\.com$",
    r"^objects\.githubusercontent\.com$",
    r"^raw\.githubusercontent\.com$",
    r"^registry\.npmjs\.org$",
    r"^huggingface\.co$",
    r"^cdn-lfs\.huggingface\.co$",
    r"^[a-z0-9.-]+\.hf\.co$",
]
PATTERNS = [re.compile(p) for p in ALLOWED]

INJECT_HOSTS = {"github.com", "api.github.com", "codeload.github.com"}
TOKEN = os.environ.get("GITHUB_TOKEN_INJECT")

# Writes are confined to the agent's own fork. Everything else on GitHub stays
# read-only, so a token that could push anywhere is narrowed at the proxy to a
# token that can only push here.
# Two shapes reach the same fork: the git transport (github.com/<owner>/<repo>.git)
# and the REST API (api.github.com/repos/<owner>/<repo>/...). The agent prefers
# the API, so covering only the git path silently blocks everything it does.
WRITE_OK = re.compile(r"^/(repos/)?agentydragon-agent/")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Pull requests must be openable against the upstream repo -- that is the whole
# point -- so allow exactly that one upstream write path.
PR_CREATE = re.compile(r"^/repos/agentydragon(-agent)?/[^/]+/pulls/?$")


def _allowed(host: str) -> bool:
    return any(p.match(host) for p in PATTERNS)


def _deny(flow: http.HTTPFlow, why: bytes) -> None:
    flow.response = http.Response.make(403, why, {"Content-Type": "text/plain"})


def http_connect(flow: http.HTTPFlow) -> None:
    if not _allowed(flow.request.host):
        _deny(flow, b"blocked by lab allowlist\n")


def request(flow: http.HTTPFlow) -> None:
    host = flow.request.host
    if not _allowed(host):
        _deny(flow, b"blocked by lab allowlist\n")
        return
    if host not in INJECT_HOSTS:
        return

    path = flow.request.path.split("?", 1)[0]
    is_write = flow.request.method in WRITE_METHODS or "git-receive-pack" in path
    if is_write and not (WRITE_OK.match(path) or PR_CREATE.match(path)):
        logger.warning("policy: refusing %s %s%s", flow.request.method, host, path)
        _deny(flow, b"blocked by lab credential policy: write outside the agent fork\n")
        return

    # Strip every credential header the client supplied before injecting.
    # Overwriting only `Authorization` leaves `x-api-key` and
    # `Proxy-Authorization` riding along untouched -- gh-aw-firewall strips
    # all three, and it is right to.
    for header in ("Authorization", "x-api-key", "Proxy-Authorization"):
        flow.request.headers.pop(header, None)

    if not TOKEN:
        # Fail closed. Forwarding unauthenticated turns a broken secret mount
        # into confusing 401s from GitHub instead of one loud local error.
        logger.error("GITHUB_TOKEN_INJECT is unset; refusing to forward")
        _deny(flow, b"credential proxy misconfigured: no token available\n")
        return
    flow.request.headers["Authorization"] = f"Bearer {TOKEN}"
