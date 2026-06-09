"""Async HTTPS GET for the public upstreams, with certifi trust + transient retry.

debian-slim ships no CA bundle, so the client is pinned to certifi's bundled
certs rather than the (absent) system trust store.
"""

from __future__ import annotations

import ssl
from collections.abc import Awaitable, Callable

import certifi
import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

HttpGet = Callable[[str, str], Awaitable[bytes]]  # (url, user_agent) -> body

# httpx raises these for network failures + non-2xx responses; callers treat them
# as per-source skips (a dead upstream shouldn't block refreshing the rest).
FETCH_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError,)

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _is_transient(exc: BaseException) -> bool:
    """A read worth retrying: a 429/5xx response, or a transport-level error."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


# 3 attempts × 120s caps a single stalled upstream at ~6min of retries — under the
# CronJob's 600s deadline, so one black-holed source can't starve the whole scrape
# (sources fetch concurrently, so the job waits ~one source's worst case, then commits
# whatever the healthy upstreams returned).
@retry(
    retry=retry_if_exception(_is_transient),
    wait=wait_exponential(multiplier=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def http_get(url: str, user_agent: str) -> bytes:
    # timeout is per-socket-op (not total), so it bounds a stalled connection without
    # killing a legitimately slow large transfer (the Zillow national CSVs are tens of MB).
    async with httpx.AsyncClient(verify=_SSL_CONTEXT, timeout=120, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": user_agent})
        resp.raise_for_status()
        return resp.content
