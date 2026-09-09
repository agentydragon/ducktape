"""Authenticated grant snapshots and transactional admission/dispatch authorization."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity
from x.agentplane.action_service.conftest import RecordingExecutor
from x.agentplane.action_service.connections import (
    ConnectionAuthority,
    Grant,
    GrantBinding,
    Identity,
    NewConnection,
    ReconnectConnection,
)
from x.agentplane.action_service.db import (
    ActionStore,
    ConnectionRow,
    ExternalGrantNotAuthorizedError,
    make_sessionmaker,
)
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionContext,
    DecisionInput,
    ExecutionResult,
    ExecutionState,
    ExternalGrantProvenance,
    Principal,
    PrincipalRole,
    ProviderOutcome,
    ProviderVerdict,
    Verdict,
)
from x.agentplane.action_service.service import ActionService

ISSUER = "https://actions.example.test"
OPERATOR = Principal(issuer="operator", subject="single", role=PrincipalRole.OPERATOR)
LEASE_DURATION = timedelta(seconds=30)


@pytest.fixture
def authority(engine: AsyncEngine) -> ConnectionAuthority:
    return ConnectionAuthority(make_sessionmaker(engine), {"personal": Identity(), "test-other": Identity()})


@pytest.fixture
def store(engine: AsyncEngine, authority: ConnectionAuthority) -> ActionStore:
    return ActionStore(make_sessionmaker(engine), external_grants=authority)


async def activated(authority: ConnectionAuthority, client_id: str) -> Grant:
    bound = await authority.bind(
        GrantBinding(
            grant_id=uuid4(),
            identity_id="personal",
            issuer=ISSUER,
            client_id=client_id,
            activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
            connection=NewConnection(display_name=client_id),
        )
    )
    return await authority.activate(bound.id)


@pytest.fixture
async def grant(authority: ConnectionAuthority) -> Grant:
    return await activated(authority, "first-client")


@pytest.fixture
def envelope() -> ActionRequestInput:
    return ActionRequestInput(
        idempotency_key="key",
        action=ActionIdentity(group="agentplane", name="echo"),
        arguments={},
        origin={"client_id": "forged", "grant_id": "forged", "issuer": "forged"},
    )


async def allow(store: ActionStore, request: ActionRequestView) -> ActionRequestView:
    result, _ = await store.decide(
        request.id,
        DecisionInput(verdict=Verdict.ALLOW, expected_version=request.version, idempotency_key="allow"),
        OPERATOR,
        provider=ActionService.HUMAN_PROVIDER,
    )
    return result


async def test_shared_identity_retry_keeps_first_snapshot_after_rename_and_revoke(
    engine: AsyncEngine, authority: ConnectionAuthority, store: ActionStore, grant: Grant, envelope: ActionRequestInput
) -> None:
    first, _ = await store.submit(envelope, grant.principal(), external_grant=grant.provenance())
    assert first.external_grant == grant.provenance()
    connection = await authority.get(grant.connection_id)
    await authority.rename(connection.id, expected_version=connection.version, display_name="renamed")
    await authority.revoke(grant.id)
    sibling = await activated(authority, "second-client")
    restarted = ActionStore(make_sessionmaker(engine), external_grants=authority)
    duplicate, created = await restarted.submit(envelope, sibling.principal(), external_grant=sibling.provenance())
    assert not created
    assert duplicate.id == first.id
    assert duplicate.external_grant == grant.provenance()
    assert (await restarted.get(first.id, OPERATOR)).external_grant == grant.provenance()
    assert duplicate.origin["client_id"] == "forged"
    assert len(await restarted.events(first.id, sibling.principal())) == 1


async def test_admission_fails_closed_on_missing_disabled_revoked_or_mismatched_authority(
    engine: AsyncEngine, authority: ConnectionAuthority, store: ActionStore, grant: Grant, envelope: ActionRequestInput
) -> None:
    invalid_snapshots: list[dict[str, object]] = [
        {"issuer": "other"},
        {"client_id": "other"},
        {"identity_id": "other"},
        {"connection_id": uuid4()},
        {"grant_id": uuid4()},
        {"revision": 2},
    ]
    for changes in invalid_snapshots:
        with pytest.raises(ExternalGrantNotAuthorizedError):
            await store.submit(
                envelope, grant.principal(), external_grant=grant.provenance().model_copy(update=changes)
            )
    with pytest.raises(ExternalGrantNotAuthorizedError):
        await store.submit(envelope, grant.principal())
    with pytest.raises(ExternalGrantNotAuthorizedError):
        await store.submit(envelope, OPERATOR, external_grant=grant.provenance())
    for checker in [None, ConnectionAuthority(make_sessionmaker(engine), {"personal": Identity(enabled=False)})]:
        with pytest.raises(ExternalGrantNotAuthorizedError):
            await ActionStore(make_sessionmaker(engine), external_grants=checker).submit(
                envelope, grant.principal(), external_grant=grant.provenance()
            )
    await authority.revoke(grant.id)
    with pytest.raises(ExternalGrantNotAuthorizedError):
        await store.submit(envelope, grant.principal(), external_grant=grant.provenance())
    assert await store.list_requests(OPERATOR) == []
    with pytest.raises(ValidationError):
        ActionRequestInput.model_validate({**envelope.model_dump(), "external_grant": grant.provenance().model_dump()})


@pytest.mark.parametrize(
    "invalidate", ["revoke", "disable", "remove", "missing_authority", "reconnect_same", "reconnect_other"]
)
async def test_original_authority_is_rechecked_before_dispatch_without_rewriting_decision(
    engine: AsyncEngine,
    authority: ConnectionAuthority,
    store: ActionStore,
    grant: Grant,
    envelope: ActionRequestInput,
    invalidate: str,
) -> None:
    request, _ = await store.submit(envelope, grant.principal(), external_grant=grant.provenance())
    allowed = await allow(store, request)
    if invalidate == "revoke":
        await authority.revoke(grant.id)
        await activated(authority, "new-authority-does-not-replace-original")
    elif invalidate == "disable":
        authority = ConnectionAuthority(make_sessionmaker(engine), {"personal": Identity(enabled=False)})
    elif invalidate == "remove":
        authority = ConnectionAuthority(make_sessionmaker(engine), {})
    elif invalidate in {"reconnect_same", "reconnect_other"}:
        connection = await authority.get(grant.connection_id)
        replacement = await authority.bind(
            GrantBinding(
                grant_id=uuid4(),
                identity_id="personal" if invalidate == "reconnect_same" else "test-other",
                issuer=ISSUER,
                client_id="test-reconnected-client",
                activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
                connection=ReconnectConnection(connection_id=connection.id, expected_version=connection.version),
            )
        )
        await authority.activate(replacement.id)
    restarted = ActionStore(
        make_sessionmaker(engine), external_grants=None if invalidate == "missing_authority" else authority
    )
    assert await restarted.claim_execution(request.id, executor_id="worker", lease_duration=LEASE_DURATION) is None
    failed = await restarted.get(request.id, OPERATOR)
    assert failed.state is ActionState.FAILED
    assert failed.decision == allowed.decision
    assert failed.external_grant == grant.provenance()
    assert failed.execution is not None
    assert failed.execution.state is ExecutionState.FAILED
    assert failed.execution.error == {"code": "external_grant_not_authorized"}
    assert failed.execution.started_at is None
    assert failed.execution.completed_at is not None
    assert [event.state for event in await restarted.events(request.id, OPERATOR)] == [
        ActionState.DECISION_PENDING,
        ActionState.ALLOWED,
        ActionState.FAILED,
    ]
    assert await restarted.claim_execution(request.id, executor_id="worker", lease_duration=LEASE_DURATION) is None
    assert await restarted.pending_dispatches() == []


async def test_claimed_work_continues_with_original_provenance_after_revocation(
    authority: ConnectionAuthority, store: ActionStore, grant: Grant, envelope: ActionRequestInput
) -> None:
    request, _ = await store.submit(envelope, grant.principal(), external_grant=grant.provenance())
    await allow(store, request)
    claim = await store.claim_execution(request.id, executor_id="worker", lease_duration=LEASE_DURATION)
    assert claim is not None
    await authority.revoke(grant.id)
    execution = await store.mark_running(request.id)
    assert execution.external_grant == grant.provenance()
    await store.finish_execution(
        request.id, claim.executor_id, claim.lease_token, ExecutionResult(state=ExecutionState.SUCCEEDED, result={})
    )
    assert (await store.get(request.id, OPERATOR)).state is ActionState.SUCCEEDED


async def test_admission_and_claim_hold_revocation_lock_until_transaction_end(
    engine: AsyncEngine, authority: ConnectionAuthority, grant: Grant, envelope: ActionRequestInput
) -> None:
    sessions = make_sessionmaker(engine)

    class CheckingAuthority:
        calls = 0

        async def authorize_action(self, session: AsyncSession, snapshot: ExternalGrantProvenance) -> bool:
            authorized = await authority.authorize_action(session, snapshot)
            assert authorized
            async with sessions.begin() as observer:
                with pytest.raises(DBAPIError, match="could not obtain lock on row"):
                    await observer.execute(
                        select(ConnectionRow)
                        .where(ConnectionRow.id == grant.connection_id)
                        .with_for_update(nowait=True)
                    )
            self.calls += 1
            return authorized

    checker = CheckingAuthority()
    store = ActionStore(sessions, external_grants=checker)
    request, _ = await store.submit(envelope, grant.principal(), external_grant=grant.provenance())
    await allow(store, request)
    assert await store.claim_execution(request.id, executor_id="worker", lease_duration=LEASE_DURATION) is not None
    assert checker.calls == 2


async def test_service_preserves_human_approval_and_canonical_external_provenance(
    store: ActionStore,
    grant: Grant,
    envelope: ActionRequestInput,
    echo_catalog: ActionCatalog,
    echo_executor: RecordingExecutor,
) -> None:
    class ExistingWorkloadProvider:
        name = "existing-workload-provider"
        called = False

        async def decide(self, context: DecisionContext) -> ProviderOutcome:
            self.called = True
            return ProviderOutcome(
                verdict=ProviderVerdict.ALLOW, reason_code="workload_autoallow", reason_description=None
            )

    provider = ExistingWorkloadProvider()
    service = ActionService(store, echo_catalog, {"agentplane": echo_executor}, providers=[provider])
    receipt = await service.submit(envelope, grant.principal(), external_grant=grant.provenance())
    assert receipt.state is ActionState.DECISION_PENDING
    assert receipt.external_grant == grant.provenance()
    assert echo_executor.requests == []
    assert not provider.called


if __name__ == "__main__":
    pytest_bazel.main()
