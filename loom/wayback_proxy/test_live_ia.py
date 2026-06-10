"""Live smoke test against the real Internet Archive (manual-only target).

Tagged manual: IA availability/rate limits make this unsuitable for CI. Run
on demand (RBE workers have open egress):

    bbr test //loom/wayback_proxy:test_live_ia --test_output=streamed
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator
from datetime import date

import aiohttp
import pytest
import pytest_bazel
from aiohttp import web

from loom.wayback_proxy.proxy import Config, start_proxy

logger = logging.getLogger(__name__)

AS_OF = date(2020, 6, 1)


def _port(runner: web.ServerRunner) -> int:
    return int(runner.addresses[0][1])


@pytest.fixture
async def live_proxy() -> AsyncIterator[str]:
    config = Config(as_of=AS_OF, upstream="https://web.archive.org", port=0)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        runner = await start_proxy(config, session, io.StringIO(), host="127.0.0.1")
        yield f"http://127.0.0.1:{_port(runner)}"
        await runner.cleanup()


async def test_live_example_com_is_clamped(live_proxy: str) -> None:
    async with aiohttp.ClientSession() as client:
        async with client.get("http://example.com/", proxy=live_proxy) as response:
            body = await response.read()
            assert response.status == 200, body.decode(errors="replace")[:500]
            ts = response.headers["X-Wayback-Timestamp"]
            logger.info("served capture ts=%s, %d bytes", ts, len(body))
            assert ts <= "20200601235959"
            assert b"Example Domain" in body

        # A pinned capture after as_of must be refused outright.
        async with client.get(
            "http://web.archive.org/web/20240101000000id_/https://example.com/", proxy=live_proxy
        ) as response:
            assert response.status == 403

        # CDX passthrough must not reveal post-as_of captures. Bounded with
        # limit=-5 (newest five): IA truncates huge unbounded listings
        # mid-stream (example.com has ~10^5 captures), cutting the JSON off.
        async with client.get(
            "http://web.archive.org/cdx/search/cdx?url=example.com/&output=json&limit=-5", proxy=live_proxy
        ) as response:
            assert response.status == 200
            rows = json.loads(await response.read())
            timestamps = [row[rows[0].index("timestamp")] for row in rows[1:]]
            assert timestamps, "expected at least one pre-as_of capture"
            assert max(timestamps) <= "20200601235959"


if __name__ == "__main__":
    pytest_bazel.main()
