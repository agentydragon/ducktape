"""mitmproxy addon that answers every flow from the clamped archive.

Running under mitmproxy gives the date-clamping proxy HTTPS for free: agents
issue ordinary ``https://`` requests, mitmproxy MITMs the TLS with its own CA
(which the sandbox trusts via ``SSL_CERT_FILE`` & friends), and the decrypted
request reaches :meth:`WaybackAddon.request` exactly like a plain ``http://``
one. No URL rewriting, no ``http://`` downgrade — the resolver is
scheme-agnostic because IA serves the same snapshot regardless of the original
scheme.

Every flow's response is set here, so the agent never reaches the live web:
the proxy is the only egress and it only ever speaks the archive.
"""

from __future__ import annotations

import logging
import math

from mitmproxy import http
from yarl import URL

from loom.wayback.proxy.proxy import (
    ClampViolationError,
    NoCaptureError,
    UpstreamError,
    UpstreamUnavailableError,
    WaybackResolver,
)

logger = logging.getLogger(__name__)

# Reserved host the proxy answers itself (used by the container healthcheck);
# never forwarded to the archive. ``.local`` keeps it off the public DNS space.
HEALTH_HOST = "wayback-proxy.local"

_TEXT = "text/plain; charset=utf-8"


def _text_response(status: int, message: str) -> http.Response:
    return http.Response.make(status, f"{message}\n".encode(), {"content-type": _TEXT})


def _retry_after_response(message: str, retry_after: float) -> http.Response:
    retry_after_seconds = str(max(0, math.ceil(retry_after)))
    return http.Response.make(503, f"{message}\n".encode(), {"content-type": _TEXT, "retry-after": retry_after_seconds})


class WaybackAddon:
    def __init__(self, resolver: WaybackResolver, health_host: str = HEALTH_HOST) -> None:
        self._resolver = resolver
        self._health_host = health_host

    async def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.pretty_host == self._health_host:
            flow.response = http.Response.make(200, b"ok\n", {"content-type": _TEXT})
            return
        target = URL(flow.request.url, encoded=True)
        try:
            result = await self._resolver.serve(target)
        except NoCaptureError as e:
            flow.response = _text_response(404, str(e))
        except ClampViolationError as e:
            flow.response = _text_response(403, str(e))
        except UpstreamUnavailableError as e:
            logger.warning("upstream unavailable for %s: %s", target, e)
            flow.response = _retry_after_response(str(e), e.retry_after)
        except UpstreamError as e:
            logger.warning("upstream error for %s: %s", target, e)
            flow.response = _text_response(502, str(e))
        else:
            flow.response = http.Response.make(result.status, result.body, result.headers)
