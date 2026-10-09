"""Connection authority against migrated PostgreSQL, including concurrent grant retries."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.api import create_app
from agentplane.action_service.auth import ConfiguredOperatorBearerAuthenticator
from agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from agentplane.action_service.connections import (
    ConnectionAuthority,
    ConnectionConflictError,
    GrantBinding,
    GrantRejectedError,
    GrantStatus,
    NewConnection,
    ReconnectConnection,
)
from agentplane.action_service.db import (
    ActionConflictError,
    ActionNotFoundError,
    ActionStore,
    ConnectionBindingChangeRow,
    ConnectionGrantRow,
    ConnectionRow,
    make_sessionmaker,
)
from agentplane.action_service.models import ActionRequestInput, OperatorPrincipal, service_account_key
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.service import ActionService
from agentplane.action_service.testing.callers import OTHER, PERSONAL, UNLABELED, admitted_callers
from agentplane.action_service.updates import ActionUpdates
from agentplane.kubernetes_watch import Freshness
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

ISSUER = "https://actions.example.test"


def binding(*, service_account: ServiceAccountRef = PERSONAL, client: str = "client-1") -> GrantBinding:
    return GrantBinding(
        grant_id=uuid4(),
        service_account=service_account,
        issuer=ISSUER,
        client_id=client,
        activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
        connection=NewConnection(display_name="My connection"),
    )


def authority(engine: AsyncEngine) -> ConnectionAuthority:
    return ConnectionAuthority(make_sessionmaker(engine), admitted_callers(PERSONAL, OTHER))


async def test_connection_stream_wakes_on_committed_grant_and_rename(engine: AsyncEngine, db_url: str) -> None:
    updates = ActionUpdates(db_url)
    service = authority(engine)
    async with updates.listener.listen():
        with updates.subscribe_connections() as subscriber:
            request = binding()
            await service.bind(request)
            async with asyncio.timeout(10):
                await subscriber.changed.wait()
            connection = (await service.list())[0]
            assert connection.grants[0].id == request.grant_id
            subscriber.changed.clear()
            await service.rename(connection.id, expected_version=connection.version, display_name="Renamed")
            async with asyncio.timeout(10):
                await subscriber.changed.wait()
            assert (await service.list())[0].display_name == "Renamed"


async def test_binding_retries_are_atomic_and_survive_authority_replacement(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    grants = await asyncio.gather(*(service.bind(request) for _ in range(5)))
    assert all(grant == grants[0] for grant in grants)
    (connection,) = await service.list()
    assert connection.grants == [grants[0]]
    assert await service.validate_pending(request.grant_id, issuer=ISSUER, client_id=request.client_id) == grants[0]
    for issuer, client_id in [("https://different.example", request.client_id), (ISSUER, "another-client")]:
        with pytest.raises(GrantRejectedError):
            await service.validate_pending(request.grant_id, issuer=issuer, client_id=client_id)
    with pytest.raises(GrantRejectedError):
        await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)
    activated = await service.activate(request.grant_id)
    assert activated.status is GrantStatus.ACTIVE
    with pytest.raises(GrantRejectedError):
        await service.validate_pending(request.grant_id, issuer=ISSUER, client_id=request.client_id)
    replacement = authority(engine)
    assert await replacement.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id) == activated
    assert await replacement.bind(request) == activated
    with pytest.raises(ConnectionConflictError):
        await replacement.bind(request.model_copy(update={"client_id": "another-client"}))
    for issuer, client_id in [("https://different.example", request.client_id), (ISSUER, "another-client")]:
        with pytest.raises(GrantRejectedError):
            await replacement.resolve(request.grant_id, issuer=issuer, client_id=client_id)


async def test_rebind_keeps_token_and_history_but_not_old_pending_action_authority(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    await service.bind(request)
    await service.activate(request.grant_id)
    before = await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)
    snapshot = before.provenance()
    connection = await service.get(before.connection_id)
    operator = OperatorPrincipal(issuer="https://operator.example", subject="test-operator")
    changed = await service.rebind(connection.id, expected_version=connection.version, caller=OTHER, operator=operator)
    assert changed.bound_caller == OTHER
    assert changed.grants[0].caller == PERSONAL  # Immutable original authorization evidence.
    assert changed.grants[0].id == before.id  # Existing OAuth token still addresses this grant.
    assert changed.version == connection.version + 1
    after = await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)
    assert after.principal().account == OTHER
    assert after.provenance().binding_version == snapshot.binding_version + 1
    async with make_sessionmaker(engine).begin() as db:
        assert not await service.authorize_action(db, snapshot)
        assert await service.authorize_action(db, after.provenance())
        stored = await db.get(ConnectionGrantRow, request.grant_id)
        assert stored is not None
        assert stored.caller == OTHER.model_dump(mode="json")  # Legacy replicas resolve the current SA.
        assert stored.original_caller == PERSONAL.model_dump(mode="json")
        log = list(await db.scalars(select(ConnectionBindingChangeRow)))
        assert len(log) == 1
        assert (log[0].operator_issuer, log[0].operator_subject) == (operator.issuer, operator.subject)
        assert log[0].previous_caller == PERSONAL.model_dump(mode="json")
        assert log[0].caller == OTHER.model_dump(mode="json")
    with pytest.raises(ConnectionConflictError):
        await service.rebind(connection.id, expected_version=connection.version, caller=PERSONAL, operator=operator)
    with pytest.raises(GrantRejectedError):
        await service.rebind(changed.id, expected_version=changed.version, caller=UNLABELED, operator=operator)
    assert (await service.get(changed.id)).bound_caller == OTHER
    await service.unbind(changed.id, expected_version=changed.version)
    with pytest.raises(GrantRejectedError):
        await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)


async def test_rebind_rejects_pending_grant_without_changing_original_caller(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    await service.bind(request)
    connection = (await service.list())[0]
    with pytest.raises(GrantRejectedError, match="pending grant"):
        await service.rebind(
            connection.id,
            expected_version=connection.version,
            caller=OTHER,
            operator=OperatorPrincipal(issuer="https://operator.example", subject="test-operator"),
        )
    assert (await service.get(connection.id)).bound_caller == PERSONAL
    async with make_sessionmaker(engine)() as db:
        grant = await db.get(ConnectionGrantRow, request.grant_id)
        assert grant is not None
        assert grant.caller == PERSONAL.model_dump(mode="json")


async def test_rebind_preserves_history_of_grant_inserted_by_legacy_replica(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    await service.bind(request)
    await service.activate(request.grant_id)
    async with make_sessionmaker(engine).begin() as db:
        grant = await db.get(ConnectionGrantRow, request.grant_id)
        assert grant is not None
        grant.original_caller = None  # Legacy replicas do not write the new column.
    connection = (await service.list())[0]
    await service.rebind(
        connection.id,
        expected_version=connection.version,
        caller=OTHER,
        operator=OperatorPrincipal(issuer="https://operator.example", subject="test-operator"),
    )
    async with make_sessionmaker(engine)() as db:
        grant = await db.get(ConnectionGrantRow, request.grant_id)
        assert grant is not None
        assert grant.original_caller == PERSONAL.model_dump(mode="json")
        assert grant.caller == OTHER.model_dump(mode="json")
    assert (await service.get(connection.id)).grants[0].caller == PERSONAL


async def test_legacy_created_connection_keeps_token_usable_and_can_rebind(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    await service.bind(request)
    await service.activate(request.grant_id)
    connection = (await service.list())[0]
    async with make_sessionmaker(engine).begin() as db:
        row = await db.get(ConnectionRow, connection.id)
        assert row is not None
        row.bound_caller = None  # A legacy replica did not know this column.
        grant = await db.get(ConnectionGrantRow, request.grant_id)
        assert grant is not None
        grant.original_caller = None
    assert (await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)).caller == PERSONAL
    legacy = await service.get(connection.id)
    assert legacy.bound_caller == PERSONAL
    rebound = await service.rebind(
        connection.id,
        expected_version=legacy.version,
        caller=OTHER,
        operator=OperatorPrincipal(issuer="https://operator.example", subject="test-operator"),
    )
    assert rebound.bound_caller == OTHER
    assert rebound.grants[0].caller == PERSONAL
    assert (await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)).caller == OTHER


async def test_legacy_reconnect_cannot_acquire_stale_connection_binding(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    await service.bind(request)
    await service.activate(request.grant_id)
    snapshot = (await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)).provenance()
    # Simulate an old replica reconnecting to a different caller: it changes the
    # grant row but knows nothing about the Connection's new bound_caller field.
    async with make_sessionmaker(engine).begin() as db:
        grant = await db.get(ConnectionGrantRow, request.grant_id)
        assert grant is not None
        grant.caller = OTHER.model_dump(mode="json")
    with pytest.raises(GrantRejectedError, match="connection and grant caller differ"):
        await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)
    async with make_sessionmaker(engine).begin() as db:
        assert not await service.authorize_action(db, snapshot)


async def test_same_service_account_shares_receipts_while_distinct_accounts_are_isolated(engine: AsyncEngine) -> None:
    service = authority(engine)
    principals = []
    submitted_grants = []
    for request in [binding(), binding(client="client-2"), binding(service_account=OTHER, client="client-3")]:
        await service.bind(request)
        submitted_grants.append(await service.activate(request.grant_id))
        principals.append(
            (await service.resolve(request.grant_id, issuer=ISSUER, client_id=request.client_id)).principal()
        )
    first, sibling, different = principals
    assert first == sibling
    assert first != different
    assert len({grant.id for grant in submitted_grants}) == 3
    assert len({grant.connection_id for grant in submitted_grants}) == 3
    assert [grant.client_id for grant in submitted_grants] == ["client-1", "client-2", "client-3"]

    store = ActionStore(make_sessionmaker(engine), external_grants=service)
    body = ActionRequestInput(
        idempotency_key="same-key",
        title="test title for same-key",
        action=ActionIdentity(group="test", name="echo"),
        arguments={},
    )
    original = await store.submit(
        body, first, request_id=uuid4(), vote=None, external_grant=submitted_grants[0].provenance()
    )
    with pytest.raises(ActionConflictError):
        await store.submit(
            body, sibling, request_id=uuid4(), vote=None, external_grant=submitted_grants[1].provenance()
        )
    assert [request.id for request in await store.list_requests(sibling, idempotency_key="same-key")] == [original.id]
    assert (await store.get(original.id, sibling)).id == original.id
    assert await store.events(original.id, sibling)
    assert len(await store.list_requests(sibling)) == 1
    assert await store.list_requests(different) == []
    with pytest.raises(ActionNotFoundError):
        await store.get(original.id, different)
    with pytest.raises(ActionNotFoundError):
        await store.events(original.id, different)
    separate = await store.submit(
        body, different, request_id=uuid4(), vote=None, external_grant=submitted_grants[2].provenance()
    )
    assert separate.id != original.id
    operator = OperatorPrincipal(issuer="operator", subject="only-operator")
    assert len(await store.list_requests(operator)) == 2


async def test_rename_unbind_and_reconnect_preserve_immutable_grants(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    original = await service.bind(request)
    active = await service.activate(original.id)
    connection = await service.get(original.connection_id)
    renamed = await service.rename(connection.id, expected_version=connection.version, display_name=" New name ")
    assert renamed.display_name == "New name"
    assert renamed.grants == [active]
    reconnect = binding(service_account=OTHER, client="client-2").model_copy(
        update={"connection": ReconnectConnection(connection_id=connection.id, expected_version=renamed.version)}
    )
    replacement = await service.bind(reconnect)
    assert replacement.connection_id == original.connection_id
    assert replacement.revision == 2
    for request_id, client_id in [(original.id, request.client_id), (replacement.id, reconnect.client_id)]:
        with pytest.raises(GrantRejectedError):
            await service.resolve(request_id, issuer=ISSUER, client_id=client_id)
    with pytest.raises(GrantRejectedError):
        await service.activate(original.id)
    await service.activate(replacement.id)
    await service.revoke(original.id)
    assert (
        await service.resolve(replacement.id, issuer=ISSUER, client_id=reconnect.client_id)
    ).status is GrantStatus.ACTIVE
    current = await service.get(connection.id)
    old, new = current.grants
    assert (old.caller, old.issuer, old.client_id, old.revision) == (PERSONAL, ISSUER, "client-1", 1)
    assert old.status is GrantStatus.REVOKED
    assert (new.caller, new.client_id) == (OTHER, "client-2")
    unbound = await service.unbind(connection.id, expected_version=current.version)
    assert all(grant.status is GrantStatus.REVOKED for grant in unbound.grants)
    assert await service.unbind(connection.id, expected_version=current.version) == unbound
    with pytest.raises(GrantRejectedError):
        await service.resolve(replacement.id, issuer=ISSUER, client_id=reconnect.client_id)


async def test_concurrent_reconnect_has_one_winner_and_stale_edits_conflict(engine: AsyncEngine) -> None:
    service = authority(engine)
    original = await service.bind(binding())
    connection = await service.get(original.connection_id)
    choice = ReconnectConnection(connection_id=connection.id, expected_version=connection.version)
    outcomes = await asyncio.gather(
        service.bind(binding().model_copy(update={"connection": choice})),
        service.bind(binding(client="other").model_copy(update={"connection": choice})),
        return_exceptions=True,
    )
    assert sum(isinstance(outcome, ConnectionConflictError) for outcome in outcomes) == 1
    current = await service.get(connection.id)
    assert len(current.grants) == 2
    assert [grant.status for grant in current.grants] == [GrantStatus.REVOKED, GrantStatus.PENDING]
    with pytest.raises(ConnectionConflictError):
        await service.rename(connection.id, expected_version=connection.version, display_name="Stale")


async def test_unlabeled_removed_unsynced_and_expired_grants_do_not_authorize(engine: AsyncEngine) -> None:
    service = authority(engine)
    request = binding()
    grant = await service.bind(request)
    async with make_sessionmaker(engine).begin() as db:
        await db.execute(
            update(ConnectionGrantRow)
            .where(ConnectionGrantRow.id == grant.id)
            .values(activation_deadline=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(GrantRejectedError):
        await service.activate(grant.id)
    expired = binding().model_copy(update={"activation_deadline": datetime.now(UTC) - timedelta(seconds=1)})
    with pytest.raises(GrantRejectedError):
        await service.bind(expired)
    unsynced = PolicyIndex(freshness=Freshness(stale_after_seconds=900))
    unsynced.service_accounts[service_account_key(PERSONAL)] = PERSONAL
    for callers in [admitted_callers(), admitted_callers(OTHER, UNLABELED), unsynced]:
        changed = ConnectionAuthority(make_sessionmaker(engine), callers)
        with pytest.raises(GrantRejectedError):
            await changed.bind(binding())
        with pytest.raises(GrantRejectedError):
            await changed.activate(grant.id)
    live = await service.bind(binding(client="still-issued"))
    await service.activate(live.id)
    unlabeled = ConnectionAuthority(make_sessionmaker(engine), admitted_callers(OTHER))
    with pytest.raises(GrantRejectedError):
        await unlabeled.resolve(live.id, issuer=ISSUER, client_id=live.client_id)


async def test_operator_routes_do_not_expose_binding_or_accept_workload_credentials(
    engine: AsyncEngine, db_url: str
) -> None:
    service = authority(engine)
    grant = await service.bind(binding())
    token = "test-operator-token"
    catalog = ActionCatalog(groups={})
    actions = ActionService(ActionStore(make_sessionmaker(engine)), catalog, {})
    app = create_app(
        actions,
        Mock(spec=WorkloadPrincipalResolver),
        ConfiguredOperatorBearerAuthenticator(token_digest=hashlib.sha256(token.encode()).digest(), subject="operator"),
        catalog,
        connections=service,
        callers=admitted_callers(),
        updates=ActionUpdates(db_url),
        direct_wait_seconds=30,
        max_wait_seconds=30,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service") as http:
        assert (await http.get("/v1/operator/connections")).status_code == 401
        assert (
            await http.get("/v1/operator/connections", headers={"Authorization": "Bearer workload"})
        ).status_code == 401
        http.headers["Authorization"] = f"Bearer {token}"
        assert (await http.get("/v1/operator/caller-service-accounts")).json() == [
            OTHER.model_dump(),
            PERSONAL.model_dump(),
        ]
        response = await http.get(f"/v1/operator/connections/{grant.connection_id}")
        assert response.status_code == 200
        assert response.json()["grants"][0]["client_id"] == "client-1"
        renamed = await http.patch(
            f"/v1/operator/connections/{grant.connection_id}", json={"display_name": "Renamed", "expected_version": 1}
        )
        assert renamed.status_code == 200
        forged = await http.patch(
            f"/v1/operator/connections/{grant.connection_id}",
            json={"display_name": "Forged", "expected_version": 2, "service_account": OTHER.model_dump()},
        )
        assert forged.status_code == 422
        endpoint = f"/v1/operator/connections/{grant.connection_id}/rebind"
        rebind = {"expected_version": 2, "service_account": OTHER.model_dump()}
        assert (await http.post(endpoint, json={**rebind, "caller": PERSONAL.model_dump()})).status_code == 422
        assert (await http.post(endpoint, json={**rebind, "expected_version": 1})).status_code == 409
        assert (await http.post(endpoint, json=rebind)).status_code == 400  # Grant is pending, not issued.
        await service.activate(grant.id)
        assert (await http.post(endpoint, json=rebind)).status_code == 409  # Activation changed the version.
        rebound = await http.post(endpoint, json={**rebind, "expected_version": 3})
        assert rebound.status_code == 200
        assert rebound.json()["bound_caller"] == OTHER.model_dump()
        assert (await service.resolve(grant.id, issuer=ISSUER, client_id=grant.client_id)).caller == OTHER
        assert (await http.post("/v1/operator/connections", json={})).status_code == 405
        unbound = await http.post(
            f"/v1/operator/connections/{grant.connection_id}/unbind", json={"expected_version": 4}
        )
        assert unbound.status_code == 200
        assert unbound.json()["grants"][0]["status"] == "revoked"


if __name__ == "__main__":
    pytest_bazel.main()
