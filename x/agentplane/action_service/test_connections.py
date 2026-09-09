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
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import update
from sqlalchemy.engine import Connection as SqlConnection
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import ConfiguredOperatorBearerAuthenticator
from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    ConnectionConflictError,
    GrantBinding,
    GrantRejectedError,
    GrantStatus,
    Identity,
    NewConnection,
    ReconnectConnection,
)
from x.agentplane.action_service.db import ActionNotFoundError, ActionStore, Base, ConnectionGrantRow, make_sessionmaker
from x.agentplane.action_service.models import ActionRequestInput, Principal, PrincipalRole
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipalResolver

ISSUER = "https://actions.example.test"


def binding(*, identity: str = "personal", client: str = "client-1") -> GrantBinding:
    return GrantBinding(
        grant_id=uuid4(),
        identity_id=identity,
        issuer=ISSUER,
        client_id=client,
        activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
        connection=NewConnection(display_name="My connection"),
    )


def authority(engine: AsyncEngine) -> ConnectionAuthority:
    return ConnectionAuthority(make_sessionmaker(engine), {"personal": Identity(), "work": Identity()})


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


async def test_same_identity_shares_receipts_while_distinct_identities_are_isolated(engine: AsyncEngine) -> None:
    service = authority(engine)
    principals = []
    submitted_grants = []
    for request in [binding(), binding(client="client-2"), binding(identity="work", client="client-3")]:
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

    store = ActionStore(make_sessionmaker(engine))
    body = ActionRequestInput(
        idempotency_key="same-key", action=ActionIdentity(group="test", name="echo"), arguments={}
    )
    original, _ = await store.submit(body, first)
    duplicate, created = await store.submit(body, sibling)
    assert duplicate.id == original.id
    assert not created
    assert (await store.get(original.id, sibling)).id == original.id
    assert await store.events(original.id, sibling)
    assert len(await store.list_requests(sibling)) == 1
    assert await store.list_requests(different) == []
    with pytest.raises(ActionNotFoundError):
        await store.get(original.id, different)
    with pytest.raises(ActionNotFoundError):
        await store.events(original.id, different)
    separate, created = await store.submit(body, different)
    assert created
    assert separate.id != original.id
    operator = Principal(issuer="operator", subject="only-operator", role=PrincipalRole.OPERATOR)
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
    reconnect = binding(identity="work", client="client-2").model_copy(
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
    assert (old.identity_id, old.issuer, old.client_id, old.revision) == ("personal", ISSUER, "client-1", 1)
    assert old.status is GrantStatus.REVOKED
    assert (new.identity_id, new.client_id) == ("work", "client-2")
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


async def test_disabled_removed_and_expired_grants_do_not_authorize(engine: AsyncEngine) -> None:
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
    for identities in [{}, {"personal": Identity(enabled=False)}]:
        changed = ConnectionAuthority(make_sessionmaker(engine), identities)
        with pytest.raises(GrantRejectedError):
            await changed.bind(binding())
        with pytest.raises(GrantRejectedError):
            await changed.activate(grant.id)
    live = await service.bind(binding(client="still-issued"))
    await service.activate(live.id)
    disabled = ConnectionAuthority(make_sessionmaker(engine), {"personal": Identity(enabled=False)})
    with pytest.raises(GrantRejectedError):
        await disabled.resolve(live.id, issuer=ISSUER, client_id=live.client_id)


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
        SandboxPrincipalAuthenticator(Mock(spec=SandboxPrincipalResolver)),
        ConfiguredOperatorBearerAuthenticator(token_digest=hashlib.sha256(token.encode()).digest(), subject="operator"),
        catalog,
        connections=service,
        updates=ActionUpdates(db_url),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://service") as http:
        assert (await http.get("/v1/operator/connections")).status_code == 401
        assert (
            await http.get("/v1/operator/connections", headers={"Authorization": "Bearer workload"})
        ).status_code == 401
        http.headers["Authorization"] = f"Bearer {token}"
        assert (await http.get("/v1/operator/identities")).json() == {
            "personal": {"enabled": True},
            "work": {"enabled": True},
        }
        response = await http.get(f"/v1/operator/connections/{grant.connection_id}")
        assert response.status_code == 200
        assert response.json()["grants"][0]["client_id"] == "client-1"
        renamed = await http.patch(
            f"/v1/operator/connections/{grant.connection_id}", json={"display_name": "Renamed", "expected_version": 1}
        )
        assert renamed.status_code == 200
        forged = await http.patch(
            f"/v1/operator/connections/{grant.connection_id}",
            json={"display_name": "Forged", "expected_version": 2, "identity_id": "work"},
        )
        assert forged.status_code == 422
        assert (await http.post("/v1/operator/connections", json={})).status_code == 405
        unbound = await http.post(
            f"/v1/operator/connections/{grant.connection_id}/unbind", json={"expected_version": 2}
        )
        assert unbound.status_code == 200
        assert unbound.json()["grants"][0]["status"] == "revoked"


def _schema_matches(connection: SqlConnection) -> None:
    context = MigrationContext.configure(
        connection,
        opts={
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                type_ != "table" or name in {"external_connection", "external_connection_grant"}
            )
        },
    )
    assert compare_metadata(context, Base.metadata) == []


async def test_connection_migration_matches_current_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(_schema_matches)


if __name__ == "__main__":
    pytest_bazel.main()
