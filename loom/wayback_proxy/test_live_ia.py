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
from yarl import URL

from loom.wayback_proxy.proxy import ClampViolationError, Config, WaybackResolver

logger = logging.getLogger(__name__)

AS_OF = date(2020, 6, 1)


@pytest.fixture
async def resolver() -> AsyncIterator[WaybackResolver]:
    config = Config(as_of=AS_OF, upstream="https://web.archive.org", port=0)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        yield WaybackResolver(config, session, io.StringIO())


async def test_live_example_com_is_clamped(resolver: WaybackResolver) -> None:
    result = await resolver.serve(URL("http://example.com/", encoded=True))
    assert result.status == 200
    ts = result.headers["X-Wayback-Timestamp"]
    logger.info("served capture ts=%s, %d bytes", ts, len(result.body))
    assert ts <= "20200601235959"
    assert b"Example Domain" in result.body

    # A pinned capture after as_of must be refused outright.
    with pytest.raises(ClampViolationError):
        await resolver.serve(URL("http://web.archive.org/web/20240101000000id_/https://example.com/", encoded=True))

    # CDX passthrough must not reveal post-as_of captures. Bounded with limit=-5
    # (newest five): IA truncates huge unbounded listings mid-stream
    # (example.com has ~10^5 captures), cutting the JSON off.
    cdx = await resolver.serve(URL("http://web.archive.org/cdx/search/cdx?url=example.com/&output=json&limit=-5"))
    assert cdx.status == 200
    rows = json.loads(cdx.body)
    timestamps = [row[rows[0].index("timestamp")] for row in rows[1:]]
    assert timestamps, "expected at least one pre-as_of capture"
    assert max(timestamps) <= "20200601235959"


if __name__ == "__main__":
    pytest_bazel.main()
