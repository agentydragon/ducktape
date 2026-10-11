"""What /healthz says about an index that has stopped moving.

The failure being pinned here is the quiet one: every watch wedged, the index frozen at whatever
it last held, and the proxy still admitting and refusing traffic against those stale rules with
nothing in its answers to say so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_bazel

from agentplane.egress.admin import create_admin_app
from agentplane.egress.decision_log import DecisionLog
from agentplane.egress.decision_store import DecisionStore, make_engine
from agentplane.egress.policy import Index
from agentplane.egress.resources import EgressBinding, EgressPolicy
from agentplane.kubernetes_watch import STALE_AFTER_CYCLES, Freshness
from agentplane.testing.fake_apiserver import binding, policy

RESYNC_SECONDS = 300
STARTED = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
KINDS = ("egresspolicies", "egressbindings", "egresscredentials", "secrets")


@dataclass
class Clock:
    """Time the test moves by hand, so staleness is asserted without waiting for it."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def index() -> Index:
    return Index(
        freshness=Freshness(stale_after_seconds=RESYNC_SECONDS * STALE_AFTER_CYCLES, at=dict.fromkeys(KINDS, STARTED)),
        synced=True,
    )


@pytest.fixture
def clock() -> Clock:
    return Clock(STARTED)


@pytest.fixture
async def session(index: Index, clock: Clock) -> AsyncIterator[httpx.AsyncClient]:
    log = DecisionLog(
        DecisionStore(make_engine("postgresql://test:test@127.0.0.1:1/test"), retention=timedelta(days=7))
    )
    app = create_admin_app(log, index, clock=clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://egress-admin.test") as client:
        yield client
    await log.close()


async def _get(session: httpx.AsyncClient) -> tuple[int, dict]:
    response = await session.get("/healthz")
    return response.status_code, response.json()


async def test_a_kind_that_stops_completing_cycles_turns_the_proxy_unhealthy(
    session: httpx.AsyncClient, index: Index, clock: Clock
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
    index.freshness.at["secrets"] = clock.now
    assert (await _get(session))[0] == 503

    for kind in KINDS:
        index.freshness.at[kind] = clock.now
    assert (await _get(session))[0] == 200


async def test_an_index_that_has_not_listed_everything_is_not_ready(session: httpx.AsyncClient, index: Index) -> None:
    """Freshness is the new half; completeness is still the old one, and both have to hold."""
    index.synced = False

    status, body = await _get(session)

    assert (status, body["synced"]) == (503, False)
    assert body["refreshedSecondsAgo"] == dict.fromkeys(KINDS, 0.0)


async def test_missing_freshness_and_drain_refuse_readiness_but_not_liveness(
    session: httpx.AsyncClient, index: Index
) -> None:
    index.freshness.at.pop("egresscredentials")
    assert (await _get(session))[0] == 503
    index.freshness.at["egresscredentials"] = STARTED
    index.draining = True
    assert (await _get(session))[0] == 503
    assert (await session.get("/livez")).status_code == 200


async def test_binding_observations_are_local_and_derived_at_read_time(
    session: httpx.AsyncClient, index: Index, clock: Clock
) -> None:
    index.bindings["test-binding"] = EgressBinding.model_validate(
        binding(
            "test-binding",
            subjects=[{"namespace": "test-namespace", "name": "test-workload"}],
            policies=["test-policy", "test-absent"],
            expires_at=(STARTED + timedelta(seconds=10)).isoformat(),
        )
    )
    index.policies["test-policy"] = EgressPolicy.model_validate(policy("test-policy", [{"hosts": ["example.test"]}]))
    body = (await session.get("/bindings")).json()
    assert body["scope"] == "replica-local"
    assert body["available"]
    assert body["bindings"][0]["reason"] == "Resolved"
    assert body["bindings"][0]["missingPolicies"] == ["test-absent"]
    clock.now += timedelta(seconds=10)
    assert (await session.get("/bindings")).json()["bindings"][0]["reason"] == "Expired"


if __name__ == "__main__":
    pytest_bazel.main()
