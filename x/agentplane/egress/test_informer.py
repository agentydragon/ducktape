"""The informer against the fake API server: sync, watch events, relist, and absence of status writes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiClient

from x.agentplane.egress.conftest import GITHUB_POLICY, SANDBOX_A, SECRET_NAME, informer
from x.agentplane.egress.policy import Index
from x.agentplane.testing.fake_apiserver import (
    BINDINGS_PLURAL,
    CREDENTIALS_PLURAL,
    POLICIES_PLURAL,
    SECRETS_PLURAL,
    FakeApiServer,
    policy,
    secret,
)

BINDING = f"{SANDBOX_A}-{GITHUB_POLICY}"


@pytest.fixture
async def index(api_client: ApiClient) -> AsyncIterator[Index]:
    index = Index()
    task = asyncio.create_task(informer(index, api_client).run())
    try:
        await index.wait_for(lambda: index.synced)
        yield index
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_initial_sync_loads_every_kind(index: Index) -> None:
    assert set(index.policies) == {GITHUB_POLICY}
    assert set(index.bindings) == {BINDING}
    assert index.secrets[SECRET_NAME].data == {"token": "real-secret-v1"}


async def test_policy_events_are_read_only(fake: FakeApiServer, index: Index) -> None:
    fake.put(POLICIES_PLURAL, policy("extra", [{"hosts": ["example.com"]}]))
    await index.wait_for(lambda: "extra" in index.policies)
    fake.delete(POLICIES_PLURAL, GITHUB_POLICY)
    await index.wait_for(lambda: GITHUB_POLICY not in index.policies)
    assert fake.status_patches == []


async def test_secret_rotation_reaches_the_index(fake: FakeApiServer, index: Index) -> None:
    fake.put(SECRETS_PLURAL, secret(SECRET_NAME, {"token": "real-secret-v2"}))
    await index.wait_for(lambda: index.secrets[SECRET_NAME].data == {"token": "real-secret-v2"})


async def test_watch_end_relists(fake: FakeApiServer, index: Index) -> None:
    """Changes made while no watch is open are picked up by the relist that follows."""
    fake.close_watches()
    fake.put(POLICIES_PLURAL, policy("late", [{"hosts": ["example.com"]}]))
    await index.wait_for(lambda: "late" in index.policies)


async def test_a_completed_cycle_is_what_advances_freshness(fake: FakeApiServer, index: Index) -> None:
    """Every kind is timestamped from the start, and a cycle the server ends moves it on.

    /healthz reads these to tell a wedged informer from a quiet one; a timestamp set anywhere but
    the end of a cycle would keep advancing through exactly the failure it has to catch.
    """
    assert set(index.refreshed) == {POLICIES_PLURAL, BINDINGS_PLURAL, CREDENTIALS_PLURAL, SECRETS_PLURAL}
    seeded = index.refreshed[POLICIES_PLURAL]

    async def end_watches_until_the_policies_cycle_completes() -> None:
        # `close_watches` ends only the watches open at that instant, and the informer is synced as
        # soon as every kind has listed — before their watches register. A policies watch that
        # registers just after the close then runs its full `resync_seconds`, which is the whole
        # budget of this test. So re-arm rather than close once; the sleep only paces the retries.
        while index.refreshed[POLICIES_PLURAL] <= seeded:
            fake.close_watches()
            await asyncio.sleep(0.01)

    # A bound so a regression fails here rather than hanging out the test target's own timeout.
    await asyncio.wait_for(end_watches_until_the_policies_cycle_completes(), timeout=10)


if __name__ == "__main__":
    pytest_bazel.main()
