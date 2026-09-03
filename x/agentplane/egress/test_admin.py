"""What /healthz says about an index that has stopped moving.

The failure being pinned here is the quiet one: every watch wedged, the index frozen at whatever
it last held, and the proxy still admitting and refusing traffic against those stale rules with
nothing in its answers to say so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
import pytest_bazel

from x.agentplane.egress.admin import create_admin_app, serve_admin
from x.agentplane.egress.decisions import DecisionRing
from x.agentplane.egress.policy import Index

RESYNC_SECONDS = 300
STARTED = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
KINDS = ("egresspolicies", "egressbindings", "sandboxes", "secrets")


@dataclass
class Clock:
    """Time the test moves by hand, so staleness is asserted without waiting for it."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def index() -> Index:
    return Index(synced=True, refreshed=dict.fromkeys(KINDS, STARTED))


@pytest.fixture
def clock() -> Clock:
    return Clock(STARTED)


@pytest.fixture
async def session(index: Index, clock: Clock) -> AsyncIterator[aiohttp.ClientSession]:
    app = create_admin_app(DecisionRing(capacity=1), index, resync_seconds=RESYNC_SECONDS, clock=clock)
    async with serve_admin(app, "127.0.0.1", 0) as port, aiohttp.ClientSession(f"http://127.0.0.1:{port}") as client:
        yield client


async def _get(session: aiohttp.ClientSession) -> tuple[int, dict]:
    async with session.get("/healthz") as response:
        return response.status, await response.json()


async def test_a_kind_that_stops_completing_cycles_turns_the_proxy_unhealthy(
    session: aiohttp.ClientSession, index: Index, clock: Clock
) -> None:
    status, body = await _get(session)
    assert (status, body["staleAfterSeconds"]) == (200, 900.0)

    # Two missed resyncs is a slow API server, not a wedge.
    clock.now = STARTED + timedelta(seconds=2 * RESYNC_SECONDS)
    assert (await _get(session))[0] == 200

    clock.now = STARTED + timedelta(seconds=4 * RESYNC_SECONDS)
    status, body = await _get(session)
    assert (status, body["synced"]) == (503, True)
    assert body["refreshedSecondsAgo"] == dict.fromkeys(KINDS, 1200.0)

    # One kind catching up is not enough while the others stay behind.
    index.refreshed["secrets"] = clock.now
    assert (await _get(session))[0] == 503

    for kind in KINDS:
        index.refreshed[kind] = clock.now
    assert (await _get(session))[0] == 200


async def test_an_index_that_has_not_listed_everything_is_not_ready(
    session: aiohttp.ClientSession, index: Index
) -> None:
    """Freshness is the new half; completeness is still the old one, and both have to hold."""
    index.synced = False

    status, body = await _get(session)

    assert (status, body["synced"]) == (503, False)
    assert body["refreshedSecondsAgo"] == dict.fromkeys(KINDS, 0.0)


if __name__ == "__main__":
    pytest_bazel.main()
