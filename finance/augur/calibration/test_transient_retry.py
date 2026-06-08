"""Tests for `with_retry_async`: it retries transient failures with backoff, gives up after the
attempt budget, and never retries non-transient errors.

The Kalshi client wiring is exercised end-to-end with a `MockTransport` that 503s a fixed
number of times before succeeding, proving the client recovers from the per-ticker 503
flapping that motivated the retry.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from finance.augur.calibration.kalshi import KalshiClient
from finance.augur.calibration.transient_retry import httpx_is_transient, with_retry_async


async def _noop_sleep(_seconds: float) -> None:
    pass


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/m")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, request=request))


async def test_retries_transient_then_succeeds() -> None:
    attempts = {"n": 0}
    slept: list[float] = []

    async def fetch() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _http_status_error(503)
        return "ok"

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    result = await with_retry_async(fetch, what="thing", is_transient=httpx_is_transient, sleep=record_sleep)
    assert result == "ok"
    assert attempts["n"] == 3
    # Exponential backoff between the two failed attempts: 0.5s, then 1.0s.
    assert slept == [0.5, 1.0]


async def test_gives_up_after_max_attempts_and_reraises_last() -> None:
    attempts = {"n": 0}

    async def fetch() -> str:
        attempts["n"] += 1
        raise _http_status_error(503)

    with pytest.raises(httpx.HTTPStatusError):
        await with_retry_async(fetch, what="thing", is_transient=httpx_is_transient, sleep=_noop_sleep)
    assert attempts["n"] == 3


async def test_non_transient_status_is_not_retried() -> None:
    attempts = {"n": 0}

    async def fetch() -> str:
        attempts["n"] += 1
        raise _http_status_error(404)

    with pytest.raises(httpx.HTTPStatusError):
        await with_retry_async(fetch, what="thing", is_transient=httpx_is_transient, sleep=_noop_sleep)
    # A 404 won't fix itself: fail immediately without burning the retry budget.
    assert attempts["n"] == 1


def test_httpx_is_transient_classification() -> None:
    assert httpx_is_transient(_http_status_error(503))
    assert httpx_is_transient(_http_status_error(429))
    assert not httpx_is_transient(_http_status_error(404))
    assert httpx_is_transient(httpx.ConnectTimeout("slow"))


async def test_kalshi_client_recovers_from_flapping_503() -> None:
    responses = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        responses["n"] += 1
        if responses["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"market": {"last_price_dollars": 0.37}})

    client = KalshiClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=_noop_sleep)
    assert (await client.get_market("KXTEST")).probability == 0.37
    assert responses["n"] == 3


if __name__ == "__main__":
    pytest_bazel.main()
