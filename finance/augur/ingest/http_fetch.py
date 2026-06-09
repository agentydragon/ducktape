"""HTTPS GET for the public upstreams, with certifi trust + transient retry.

debian-slim ships no CA bundle, so the SSL context is pinned to certifi's
(botocore bundles its own certs for S3, so only the upstream fetch needs this).
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from collections.abc import Callable

import certifi
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

HttpGet = Callable[[str, str], bytes]  # (url, user_agent) -> body

# urllib raises these for network failures; callers treat them as per-source skips.
FETCH_ERRORS: tuple[type[BaseException], ...] = (urllib.error.URLError, TimeoutError)

_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _is_transient(exc: BaseException) -> bool:
    """A read worth retrying: a 429/5xx response, or a transport-level timeout/URL error."""
    if isinstance(exc, urllib.error.HTTPError):  # HTTPError is a URLError subclass — check it first
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, TimeoutError | urllib.error.URLError)


@retry(
    retry=retry_if_exception(_is_transient),
    wait=wait_exponential(multiplier=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def http_get(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    # timeout is generous: the Zillow national CSVs are tens of MB.
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CONTEXT) as resp:
        return bytes(resp.read())
