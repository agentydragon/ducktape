"""Authenticated grant snapshots and transactional admission/dispatch authorization."""

import asyncio
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
    GrantRejectedError,
    GrantStatus,
    NewConnection,
    ReconnectConnection,
)
from x.agentplane.action_service.db import (
    ActionStore,
    ConnectionGrantRow,
    ConnectionRow,
    ExternalGrantNotAuthorizedError,
    make_sessionmaker,
)
from x.agentplane.action_service.models import (
    CONFIGURED_IDENTITY_ISSUER,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    ConfiguredIdentityRef,
    DecisionInput,
    ExecutionResult,
    ExecutionState,
    ExternalGrantProvenance,
    Principal,
    PrincipalRole,
    ProviderOutcome,
    ProviderVerdict,
    ServiceAccountCaller,
    ServiceAccountRef,
    Verdict,
)
from x.agentplane.action_service.policy_evaluation import PROVIDER_NAME, PolicySetDecisionProvider
from x.agentplane.action_service.policy_informer import PolicyIndex, namespaced_key
from x.agentplane.action_service.policy_resources import parse_binding, parse_policy_set
from x.agentplane.action_service.providers import DecisionContext
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.test_fixtures.callers import OTHER, PERSONAL, eligible_callers

ISSUER = "https://actions.example.test"
OPERATOR = Principal(issuer="operator", subject="single", role=PrincipalRole.OPERATOR)
LEASE_DURATION = timedelta(seconds=30)


@pytest.fixture
def authority(engine: AsyncEngine) -> ConnectionAuthority:
    return ConnectionAuthority(make_sessionmaker(engine), eligible_callers(PERSONAL, OTHER))


@pytest.fixture
def store(engine: AsyncEngine, authority: ConnectionAuthority) -> ActionStore:
    return ActionStore(make_sessionmaker(engine), external_grants=authority)


async def activated_as(authority: ConnectionAuthority, service_account: ServiceAccountRef, client_id: str) -> Grant:
    bound = await authority.bind(
        GrantBinding(
            grant_id=uuid4(),
            service_account=service_account,
            issuer=ISSUER,
            client_id=client_id,
            activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
            connection=NewConnection(display_name=client_id),
        )
    )
    return await authority.activate(bound.id)


async def activated(authority: ConnectionAuthority, client_id: str) -> Grant:
    return await activated_as(authority, PERSONAL, client_id)


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


async def test_shared_service_account_retry_keeps_first_snapshot_after_rename_and_revoke(
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
        {"caller": OTHER},
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
    for checker in [None, ConnectionAuthority(make_sessionmaker(engine), eligible_callers(OTHER))]:
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
    "invalidate", ["revoke", "unlabel", "remove", "missing_authority", "reconnect_same", "reconnect_other"]
)
@pytest.mark.parametrize("available", [True, False])
async def test_original_authority_is_rechecked_before_dispatch_without_rewriting_decision(
    engine: AsyncEngine,
    authority: ConnectionAuthority,
    store: ActionStore,
    grant: Grant,
    envelope: ActionRequestInput,
    invalidate: str,
    available: bool,
) -> None:
    request, _ = await store.submit(envelope, grant.principal(), external_grant=grant.provenance())
    allowed = await allow(store, request)
    if invalidate == "revoke":
        await authority.revoke(grant.id)
        await activated(authority, "new-authority-does-not-replace-original")
    elif invalidate == "unlabel":
        authority = ConnectionAuthority(make_sessionmaker(engine), eligible_callers(OTHER))
    elif invalidate == "remove":
        authority = ConnectionAuthority(make_sessionmaker(engine), eligible_callers())
    elif invalidate in {"reconnect_same", "reconnect_other"}:
        connection = await authority.get(grant.connection_id)
        replacement = await authority.bind(
            GrantBinding(
                grant_id=uuid4(),
                service_account=PERSONAL if invalidate == "reconnect_same" else OTHER,
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
    assert (
        await restarted.claim_execution(
            request.id, executor_id="worker", lease_duration=LEASE_DURATION, can_dispatch=lambda identity: available
        )
        is None
    )
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
    assert (
        await restarted.claim_execution(
            request.id, executor_id="worker", lease_duration=LEASE_DURATION, can_dispatch=lambda identity: available
        )
        is None
    )
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


async def test_pre_service_account_grant_stays_readable_but_never_authorizes(
    engine: AsyncEngine, authority: ConnectionAuthority, store: ActionStore, envelope: ActionRequestInput
) -> None:
    """Rows migrated from configured Identities keep their history and provenance; only fresh OAuth
    selecting a ServiceAccount regains authority."""
    now = datetime.now(UTC)
    connection_id, grant_id = uuid4(), uuid4()
    async with make_sessionmaker(engine).begin() as db:
        db.add(ConnectionRow(id=connection_id, display_name="Legacy", version=2, created_at=now, updated_at=now))
        await db.flush()
        db.add(
            ConnectionGrantRow(
                id=grant_id,
                connection_id=connection_id,
                revision=1,
                caller={"identity_id": "legacy-personal"},
                issuer=ISSUER,
                client_id="legacy-client",
                request_digest="legacy-digest",
                activation_deadline=now + timedelta(minutes=10),
                status=GrantStatus.ACTIVE,
                created_at=now,
                activated_at=now,
                revoked_at=None,
            )
        )
    (legacy,) = (await authority.get(connection_id)).grants
    assert legacy.caller == ConfiguredIdentityRef(identity_id="legacy-personal")
    assert legacy.principal().issuer == CONFIGURED_IDENTITY_ISSUER
    with pytest.raises(GrantRejectedError):
        await authority.resolve(grant_id, issuer=ISSUER, client_id="legacy-client")
    with pytest.raises(ExternalGrantNotAuthorizedError):
        await store.submit(envelope, legacy.principal(), external_grant=legacy.provenance())
    persisted = ExternalGrantProvenance.model_validate(
        {
            "identity_id": "legacy-personal",
            "issuer": ISSUER,
            "client_id": "legacy-client",
            "connection_id": str(connection_id),
            "grant_id": str(grant_id),
            "revision": 1,
        }
    )
    assert persisted == legacy.provenance()


async def test_service_preserves_human_approval_and_canonical_external_provenance(
    store: ActionStore,
    grant: Grant,
    envelope: ActionRequestInput,
    echo_catalog: ActionCatalog,
    echo_executor: RecordingExecutor,
) -> None:
    """Providers see an external submission as its ServiceAccount caller; the provenance rides
    through to the executor untouched."""

    class RecordingProvider:
        name = "recording-provider"

        def __init__(self) -> None:
            self.contexts: list[DecisionContext] = []

        async def decide(self, context: DecisionContext) -> ProviderOutcome:
            self.contexts.append(context)
            return ProviderOutcome(verdict=ProviderVerdict.ALLOW, reason_code="scripted_allow", reason_description=None)

    provider = RecordingProvider()
    service = ActionService(store, echo_catalog, {"agentplane": echo_executor}, providers=[provider])
    try:
        receipt = await service.submit(envelope, grant.principal(), external_grant=grant.provenance())
        assert receipt.state is ActionState.ALLOWED
        assert receipt.external_grant == grant.provenance()
        (context,) = provider.contexts
        assert context.caller == ServiceAccountCaller(service_account=PERSONAL, grant_revision=grant.revision)
        assert context.bindings == ()
        async with asyncio.timeout(10):
            view = await service.get(receipt.id, grant.principal())
            while view.state is not ActionState.SUCCEEDED:
                await asyncio.sleep(0.01)
                view = await service.get(receipt.id, grant.principal())
    finally:
        await service.close()
    (executed,) = echo_executor.requests
    assert executed.external_grant == grant.provenance()
    assert executed.caller_principal == grant.principal().key


async def test_bound_service_account_is_auto_approved_by_its_binding_only(
    engine: AsyncEngine,
    authority: ConnectionAuthority,
    store: ActionStore,
    grant: Grant,
    envelope: ActionRequestInput,
    echo_catalog: ActionCatalog,
    echo_executor: RecordingExecutor,
) -> None:
    namespace = PERSONAL.namespace
    index = PolicyIndex(synced=True)
    index.policy_sets[namespaced_key(namespace, "echo")] = parse_policy_set(
        {
            "metadata": {"name": "echo", "namespace": namespace, "uid": "u1", "generation": 1, "resourceVersion": "1"},
            "spec": {"autoApproveIf": [{"type": "exact_actions", "actions": {"agentplane": ["echo"]}}]},
        }
    )
    index.bindings[namespaced_key(namespace, "personal-echo")] = parse_binding(
        {
            "metadata": {
                "name": "personal-echo",
                "namespace": namespace,
                "uid": "u2",
                "generation": 1,
                "resourceVersion": "2",
            },
            "spec": {"subject": {"serviceAccount": PERSONAL.model_dump()}, "policySets": ["echo"]},
        }
    )
    service = ActionService(
        store, echo_catalog, {"agentplane": echo_executor}, providers=[PolicySetDecisionProvider()], policies=index
    )
    try:
        allowed = await service.submit(envelope, grant.principal(), external_grant=grant.provenance())
        assert allowed.state is ActionState.ALLOWED
        assert allowed.decision is not None
        assert allowed.decision.provider == PROVIDER_NAME
        assert allowed.decision.policy_evidence is not None
        assert [binding.name for binding in allowed.decision.policy_evidence.bindings] == ["personal-echo"]
        other = await activated_as(authority, OTHER, "other-client")
        unbound = await service.submit(envelope, other.principal(), external_grant=other.provenance())
        assert unbound.state is ActionState.DECISION_PENDING
        assert unbound.decision is None
    finally:
        await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
