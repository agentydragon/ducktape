"""Subscription HTTP operations and service-owned worker lifetime."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import pytest
import pytest_bazel

from agentplane.action_service.models import ActionEventView, ActionState
from agentplane.notification_service.api import authenticated_caller, create_app, sandbox_status_frames
from agentplane.notification_service.models import (
    ActionsSource,
    DestinationRef,
    Subscribe,
    SubscriptionStatus,
    SubscriptionView,
)
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings
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
        assert response.json()["github"] is None
        assert "paused" not in response.json()
        assert response.json()["source"] == subscription.source.model_dump(mode="json")
        assert (await client.get("/v1/subscriptions")).json() == [response.json()]
        assert (await client.get(path)).json() == response.json()
        assert (await client.patch(path, json={"version": 1})).status_code == 409
        assert (await client.delete(path)).json()["cancelled"]
        assert (await client.patch(path, json={"version": 3})).status_code == 409
    for model in ["Subscribe", "SubscriptionUpdate", "SubscriptionView"]:
        assert "paused" not in app.openapi()["components"]["schemas"][model]["properties"]


@pytest.mark.parametrize("model", [SubscriptionView, SubscriptionStatus])
def test_github_status_field_is_required_but_nullable(model: type[SubscriptionView] | type[SubscriptionStatus]) -> None:
    schema = model.model_json_schema()
    assert "github" in schema["required"]
    assert "default" not in schema["properties"]["github"]
    assert {"type": "null"} in schema["properties"]["github"]["anyOf"]


async def test_lifespan_owns_workers_and_readiness_tracks_failure_and_shutdown(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    service = create_autospec(Service, instance=True)
    service.github = None
    service.store = store
    workers_running = asyncio.Event()
    fail = asyncio.Event()
    failed = asyncio.Event()
    park = asyncio.Event()
    private_message = "delivery secret should not appear in logs"
    started: set[int] = set()
    stopped: set[int] = set()

    async def run() -> None:
        index = len(started)
        started.add(index)
        workers_running.set()
        try:
            if index == 0:
                await fail.wait()
                raise RuntimeError(private_message)
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
            await asyncio.sleep(0)  # Let the task completion callback report the failure.
            assert (await client.get("/readyz")).status_code == 503
            assert (await client.get("/healthz")).status_code == 503
            assert "notification worker notifications-0 failed: RuntimeError" in caplog.text
            assert "raise RuntimeError(private_message)" in caplog.text
            assert private_message not in caplog.text
        assert stopped == started
        assert not store.wakeups.listener.connected
        assert (await client.get("/readyz")).status_code == 503
        assert not any("notifications-1" in record.message for record in caplog.records)


async def test_worker_return_is_fatal_without_a_traceback(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    service = create_autospec(Service, instance=True)
    service.store = store
    returned = asyncio.Event()

    async def run() -> None:
        returned.set()

    service.run.side_effect = run
    app = create_app(service, create_autospec(WorkloadPrincipalResolver, instance=True))
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notifications.test") as client,
        app.router.lifespan_context(app),
    ):
        await returned.wait()
        await asyncio.sleep(0)
        assert (await client.get("/healthz")).status_code == 503
        assert "notification worker notifications-0 stopped unexpectedly" in caplog.text


async def test_operator_status_is_read_only_and_uid_pinned(store: Store) -> None:
    service = create_autospec(Service, instance=True)
    service.store = store
    service.sandboxes = SimpleNamespace(namespace="test")
    service.operator_reader_account = "app"
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=60, max_wait_seconds=120)
    app = create_app(service, create_autospec(WorkloadPrincipalResolver, instance=True))
    principal = PRINCIPAL
    app.dependency_overrides[authenticated_caller] = lambda: principal
    subscription: SubscriptionView = await store.subscribe(
        PRINCIPAL,
        Subscribe(
            destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
            session_id="session",
            idempotency_key="status",
            source=ActionsSource(provider="actions", request_id=uuid4()),
        ),
    )
    claim = await store.claim()
    assert claim is not None
    source = await store.source(claim)
    assert source is not None
    await store.record(
        claim, source, [ActionEventView(sequence=1, state=ActionState.DECISION_PENDING, at=datetime.now(UTC))]
    )
    url = "/operator/v1/sandboxes/test/sandbox/notifications"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://notifications.test"
    ) as client:
        assert (await client.get(url, params={"uid": "sandbox-uid"})).status_code == 403
        assert (await client.get(url + "/stream", params={"uid": "sandbox-uid"})).status_code == 403
        app.dependency_overrides[authenticated_caller] = lambda: WorkloadPrincipal(
            "test", "app", "system:serviceaccount:test:app", "app-pod", "pod-uid"
        )
        assert (await client.get(url, params={"uid": "another-uid"})).json()["inboxes"] == []
        response = await client.get(url, params={"uid": "sandbox-uid"})
        assert response.status_code == 200
        inbox = response.json()["inboxes"][0]
        assert inbox["unannounced_count"] == 1
        assert inbox["pending_acknowledgement_count"] == 1
        assert inbox["pending_entries_more"] is False
        assert isinstance(subscription.source, ActionsSource)
        assert [(entry["cursor"], entry["summary"]) for entry in inbox["pending_entries"]] == [
            (1, f"Action {subscription.source.request_id} · event 1 · decision_pending")
        ]
        assert "payload" not in str(inbox)
        await store.acknowledge(PRINCIPAL.account, subscription.inbox_id, 1)
        acknowledged = (await client.get(url, params={"uid": "sandbox-uid"})).json()["inboxes"][0]
        assert acknowledged["pending_entries"] == []
        assert acknowledged["pending_acknowledgement_count"] == 0
        assert inbox["subscriptions"][0]["id"] == str(subscription.id)
        assert inbox["notice_wait_reason"] == "debouncing"
        assert "payload" not in str(response.json())
        assert (await client.get("/v1/inboxes")).json() == []  # App identity gets no agent inbox authority.


async def test_status_stream_replays_snapshot_and_follows_committed_changes(store: Store) -> None:
    service = create_autospec(Service, instance=True)
    service.store = store
    service.notice_debounce = NoticeDebounceSettings(quiet_seconds=60, max_wait_seconds=120)
    async with store.wakeups.listener.listen():
        frames = sandbox_status_frames(service, "test", "sandbox", "sandbox-uid")
        try:
            initial = await asyncio.wait_for(anext(frames), 5)
            assert b'"inboxes": []' in initial
            sub = await store.subscribe(
                PRINCIPAL,
                Subscribe(
                    destination_ref=DestinationRef(namespace="test", name="sandbox", uid="sandbox-uid"),
                    session_id="stream",
                    idempotency_key="stream",
                    source=ActionsSource(provider="actions", request_id=uuid4()),
                ),
            )
            updated = await asyncio.wait_for(anext(frames), 5)
            assert str(sub.inbox_id).encode() in updated
            await store.change(PRINCIPAL.account, sub.id, None)
            cancelled = await asyncio.wait_for(anext(frames), 5)
            assert b'"cancelled": true' in cancelled
        finally:
            await frames.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
