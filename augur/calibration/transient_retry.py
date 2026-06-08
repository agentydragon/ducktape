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

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt, wait_exponential

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


async def with_retry_async[T](
    fetch: Callable[[], Awaitable[T]],
    *,
    what: str,
    is_transient: Callable[[BaseException], bool],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Await ``fetch`` via tenacity, retrying with exponential backoff only failures for
    which ``is_transient`` returns True; any other exception (and the final transient one,
    once the attempt budget is spent) propagates. ``what`` labels the resource in the log.
    """

    def _log_retry(retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
        logger.warning(
            "transient error fetching %s (attempt %d/%d): %r; retrying",
            what,
            retry_state.attempt_number,
            max_attempts,
            exc,
        )

    retrying = AsyncRetrying(
        retry=retry_if_exception(is_transient),
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=backoff_base_seconds, exp_base=2),
        sleep=sleep,
        before_sleep=_log_retry,
        reraise=True,
    )

    # tenacity only awaits the retried callable when it's a coroutine *function*; `fetch` is often a
    # plain lambda returning a coroutine (so the client can bind args), which tenacity would call but
    # not await. Wrap it in an `async def` so each attempt is awaited.
    async def _attempt() -> T:
        return await fetch()

    return await retrying(_attempt)
