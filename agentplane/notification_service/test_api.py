"""Subscription HTTP operations and service-owned worker lifetime."""

import asyncio
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import pytest_bazel

from agentplane.notification_service.api import authenticated_caller, create_app
from agentplane.notification_service.models import ActionsSource, DestinationRef, Subscribe
from agentplane.notification_service.service import Service
from agentplane.notification_service.store import Store
from agentplane.workload_auth.principal import WorkloadPrincipal, WorkloadPrincipalResolver

PRINCIPAL = WorkloadPrincipal(
    namespace="test",
    service_account_name="agent",
    service_account_subject="system:serviceaccount:test:agent",
    pod_name="sandbox",
    pod_uid="pod-uid",
)


async def test_subscription_patch_renews_without_pause(store: Store) -> None:
    service = create_autospec(Service, instance=True)
    service.github = None
    service.store = store
    resolver = create_autospec(WorkloadPrincipalResolver, instance=True)
    app = create_app(service, resolver)
    app.dependency_overrides[authenticated_caller] = lambda: PRINCIPAL
    subscription = await store.subscribe(
        PRINCIPAL,
        Subscribe(
            destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
            session_id="session",
            idempotency_key="renew",
            source=ActionsSource(provider="actions", request_id=uuid4()),
        ),
    )
    path = f"/v1/subscriptions/{subscription.id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://notifications.test"
    ) as client:
        for paused in [True, False]:
            response = await client.patch(path, json={"version": 1, "paused": paused})
            assert response.status_code == 422
        response = await client.patch(path, json={"version": 1, "lifetime_days": 30})
        assert response.status_code == 200
        assert response.json()["version"] == 2
        assert "paused" not in response.json()
        assert response.json()["source"] == subscription.source.model_dump(mode="json")
        assert (await client.get("/v1/subscriptions")).json() == [response.json()]
        assert (await client.get(path)).json() == response.json()
        assert (await client.patch(path, json={"version": 1})).status_code == 409
        assert (await client.delete(path)).json()["cancelled"]
        assert (await client.patch(path, json={"version": 3})).status_code == 409
    for model in ["Subscribe", "SubscriptionUpdate", "SubscriptionView"]:
        assert "paused" not in app.openapi()["components"]["schemas"][model]["properties"]


async def test_lifespan_owns_workers_and_readiness_tracks_failure_and_shutdown(store: Store) -> None:
    service = create_autospec(Service, instance=True)
    service.github = None
    service.store = store
    workers_running = asyncio.Event()
    fail = asyncio.Event()
    failed = asyncio.Event()
    park = asyncio.Event()
    started: set[int] = set()
    stopped: set[int] = set()

    async def run() -> None:
        index = len(started)
        started.add(index)
        workers_running.set()
        try:
            if index == 0:
                await fail.wait()
                raise RuntimeError("worker failed")
            await park.wait()
        finally:
            stopped.add(index)
            if index == 0:
                failed.set()

    service.run.side_effect = run
    app = create_app(service, create_autospec(WorkloadPrincipalResolver, instance=True))
    async with (
        asyncio.timeout(10),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notifications.test") as client,
    ):
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/healthz")).status_code == 200
        async with app.router.lifespan_context(app):
            await workers_running.wait()
            assert (await client.get("/readyz")).status_code == 200
            fail.set()
            await failed.wait()
            assert (await client.get("/readyz")).status_code == 503
            assert (await client.get("/healthz")).status_code == 200
        assert stopped == started
        assert not store.wakeups.listener.connected
        assert (await client.get("/readyz")).status_code == 503


if __name__ == "__main__":
    pytest_bazel.main()
