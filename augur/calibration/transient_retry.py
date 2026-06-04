"""Bounded retry-with-backoff for the flaky read-only prediction-market APIs.

The read-only PM endpoints intermittently fail for individual markets — Kalshi in
particular returns `503 Service Unavailable` for a ticker one moment and `200` the next.
A calibration run fetches the whole catalog, so a single transient 5xx otherwise drops
that market's row entirely (Kalshi was dropping *every* row this way, leaving only
Manifold surfaced). Retry the network read a few times with exponential backoff before
giving up; on final failure the caller's existing handling drops the row as before.

TODO(2026-06-04): The per-process TTL cache + inline retry in the clients is a stopgap.
The right architecture is a small separate syncer that continuously mirrors Kalshi /
Manifold / Polymarket market states into a local store with a bounded max staleness, and
has the server read from that store instead of hitting the upstreams inline on every
calibration run. That decouples calibration latency and availability from the PM APIs'
flakiness entirely. Tracked in augur/TODO.md.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

# Initial attempt + 2 retries, backing off 0.5s then 1.0s. Bounded so a wedged upstream
# can't stall a whole-catalog calibration run for long.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5


def httpx_is_transient(exc: BaseException) -> bool:
    """A raw-httpx failure worth retrying: a 5xx/429 response, or a transport-level error
    (connect/read timeout, connection reset). 4xx and response-parsing errors won't fix
    themselves on a retry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


def with_retry[T](
    fetch: Callable[[], T],
    *,
    what: str,
    retry_on: type[BaseException] | tuple[type[BaseException], ...],
    is_transient: Callable[[BaseException], bool],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fetch`` up to ``max_attempts`` times, retrying with exponential backoff only
    those exceptions in ``retry_on`` for which ``is_transient`` returns True. Any other
    exception (and the final transient one) propagates to the caller.

    ``what`` is a short human label for the resource being fetched, used in the retry log.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fetch()
        except retry_on as exc:
            if attempt >= max_attempts or not is_transient(exc):
                raise
            backoff = backoff_base_seconds * 2 ** (attempt - 1)
            logger.warning(
                "transient error fetching %s (attempt %d/%d): %r; retrying in %.1fs",
                what,
                attempt,
                max_attempts,
                exc,
                backoff,
            )
            sleep(backoff)
    raise AssertionError("unreachable: retry loop exited without return or raise")
