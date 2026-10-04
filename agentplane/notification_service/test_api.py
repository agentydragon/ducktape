"""HTTP dependency wiring and app-owned worker lifetime, without duplicating delivery tests."""

import asyncio
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import pytest_bazel

from agentplane.notification_service.api import authenticated_caller, create_app, notification_service
from agentplane.notification_service.models import ActionsSource, DestinationRef, Subscribe
from agentplane.notification_service.service import Service
from agentplane.notification_service.store import ConflictError, QuotaError, Store
from agentplane.workload_auth.principal import WorkloadPrincipal, WorkloadPrincipalResolver

PRINCIPAL = WorkloadPrincipal(
    namespace="test",
    service_account_name="agent",
    service_account_subject="system:serviceaccount:test:agent",
    pod_name="sandbox",
    pod_uid="pod-uid",
)


async def test_dependency_overrides_are_app_local_and_not_wire_parameters() -> None:
    service = create_autospec(Service, instance=True)
    service.subscribe.side_effect = QuotaError("inbox limit")
    override = create_autospec(Service, instance=True)
    override.subscribe.side_effect = ConflictError("creation key conflict")
    resolver = create_autospec(WorkloadPrincipalResolver, instance=True)
    resolver.resolve_workload.return_value = PRINCIPAL
    app = create_app(service, resolver)
    other = create_app(service, resolver)
    app.dependency_overrides[authenticated_caller] = lambda: PRINCIPAL
    app.dependency_overrides[notification_service] = lambda: override
    body = {
        "destination_ref": {"namespace": "test", "name": "sandbox", "uid": "sandbox-uid"},
        "session_id": "session",
        "idempotency_key": "listen",
        "source": {"provider": "actions", "request_id": str(uuid4())},
    }
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notifications.test") as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=other), base_url="http://notifications.test"
        ) as independent,
    ):
        discovery = await client.get("/v1/providers")
        assert discovery.status_code == 200
        assert discovery.json() == {
            "actions": {
                "subscription_schema": Subscribe.model_json_schema(),
                "content": "ActionEventView: sequence, state, at, actor",
            }
        }
        response = await client.post("/v1/subscriptions", json=body)
        assert response.status_code == 409
        assert response.json() == {"detail": "creation key conflict"}
        override.subscribe.assert_awaited_once_with(PRINCIPAL, Subscribe.model_validate(body))
        service.subscribe.assert_not_awaited()
        resolver.resolve_workload.assert_not_awaited()
        assert (await independent.get("/v1/providers")).status_code == 401
        response = await independent.post(
            "/v1/subscriptions", json=body, headers={"Authorization": "Bearer test-workload"}
        )
        assert response.status_code == 429
        assert response.json() == {"detail": "inbox limit"}
        resolver.resolve_workload.assert_awaited_once_with("test-workload")
        service.subscribe.assert_awaited_once_with(PRINCIPAL, Subscribe.model_validate(body))
        legacy = body.copy()
        legacy["client_key"] = legacy.pop("idempotency_key")
        assert (await client.post("/v1/subscriptions", json=legacy)).status_code == 422
        # The old name is not accepted alongside the new one either.
        response = await client.post("/v1/subscriptions", json=body | {"client_key": "different"})
        assert response.status_code == 422
        for invalid in [
            body | {"provider": "actions"},
            body | {"request_id": str(uuid4())},
            body | {"after_sequence": 0},
            {key: value for key, value in body.items() if key != "source"},
            body | {"source": {"request_id": str(uuid4())}},
            body | {"source": {"provider": "github", "repository": "agentydragon/ducktape"}},
            body | {"source": {"provider": "actions", "request_id": str(uuid4()), "repository": "extra"}},
        ]:
            assert (await client.post("/v1/subscriptions", json=invalid)).status_code == 422
        override.subscribe.assert_awaited_once()
        app.dependency_overrides.clear()
        assert (await client.post("/v1/subscriptions", json=body)).status_code == 401
    schema = app.openapi()
    for model in ["Subscribe", "SubscriptionView"]:
        assert "idempotency_key" in schema["components"]["schemas"][model]["properties"]
        assert "client_key" not in schema["components"]["schemas"][model]["properties"]
        properties = schema["components"]["schemas"][model]["properties"]
        assert not {"provider", "request_id", "after_sequence"} & properties.keys()
        assert properties["source"] == {"$ref": "#/components/schemas/Source"}
    for name, variant in [("Source", "ActionsSource"), ("EventIdentity", "ActionsEvent")]:
        union = schema["components"]["schemas"][name]
        assert union["discriminator"] == {
            "propertyName": "provider",
            "mapping": {"actions": f"#/components/schemas/{variant}"},
        }
        assert union["oneOf"] == [{"$ref": f"#/components/schemas/{variant}"}]
    entry = schema["components"]["schemas"]["EntryView"]["properties"]
    assert entry["event"] == {"$ref": "#/components/schemas/EventIdentity"}
    assert not {"request_id", "source_sequence"} & entry.keys()
    discovery_operation = schema["paths"]["/v1/providers"]["get"]
    provider_response = discovery_operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert provider_response["additionalProperties"] == {"$ref": "#/components/schemas/ProviderView"}
    operation = schema["paths"]["/v1/subscriptions"]["post"]
    assert not operation.get("parameters")
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Subscribe"
    }


async def test_subscription_patch_renews_without_pause(store: Store) -> None:
    service = create_autospec(Service, instance=True)
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
    service.store = store
    started = asyncio.Event()
    fail = asyncio.Event()
    failed = asyncio.Event()
    park = asyncio.Event()
    count = 0
    stopped: set[int] = set()

    async def run() -> None:
        nonlocal count
        index = count
        count += 1
        if count == 4:
            started.set()
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
            await started.wait()
            assert service.run.await_count == 4
            assert (await client.get("/readyz")).status_code == 200
            fail.set()
            await failed.wait()
            assert (await client.get("/readyz")).status_code == 503
            assert (await client.get("/healthz")).status_code == 200
        assert stopped == {0, 1, 2, 3}
        assert (await client.get("/readyz")).status_code == 503


if __name__ == "__main__":
    pytest_bazel.main()
