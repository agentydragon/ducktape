"""Canonical ActionRequest state and its transactional lifecycle transitions over the Action Service's rows."""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.connections import ConnectionAuthority
from agentplane.action_service.db import (
    ActionEventRow,
    ActionRequestRow,
    DecisionRow,
    ExecutionRow,
    ExecutorHeartbeatRow,
    SessionMaker,
)
from agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CallerPrincipal,
    CancellationOutcome,
    CancellationResult,
    DecisionInput,
    DecisionView,
    ExecutionClaim,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    ExecutionView,
    ExternalGrantProvenance,
    OperatorPrincipal,
    PolicyEvidence,
    Principal,
    ProviderVerdict,
    ProviderVote,
    ReadPrincipal,
    ReconciliationSource,
    ServiceReaderPrincipal,
    UnknownOutcomeReason,
    Verdict,
    operator_or_none,
)
from agentplane.subjects import ServiceAccountRef


class ActionNotFoundError(Exception):
    pass


class ActionConflictError(Exception):
    pass


class ExternalGrantNotAuthorizedError(Exception):
    pass


_TERMINAL_ACTION_STATE = {
    ExecutionState.SUCCEEDED: ActionState.SUCCEEDED,
    ExecutionState.FAILED: ActionState.FAILED,
    ExecutionState.CANCELLED: ActionState.CANCELLED,
    ExecutionState.EXECUTION_UNKNOWN: ActionState.EXECUTION_UNKNOWN,
}

_SECRET_KEYS = frozenset(
    {
        # keep-sorted start
        "accesstoken",
        "apikey",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "setcookie",
        "token",
        # keep-sorted end
    }
)


class ActionStore:
    def __init__(self, sessions: SessionMaker, *, external_grants: ConnectionAuthority) -> None:
        self._sessions = sessions
        self._external_grants = external_grants

    async def submit(
        self,
        body: ActionRequestInput,
        principal: CallerPrincipal,
        *,
        request_id: UUID,
        vote: ProviderVote | None,
        external_grant: ExternalGrantProvenance | None = None,
    ) -> ActionRequestView:
        """Persist an admitted request; ActionService resolves its group/action before calling here.

        A decisive `vote` from admission's provider evaluation commits with the insert, so the
        request is never visible as pending: `decision_pending` only ever means waiting for a human.

        A presented grant must still authorize this exact caller. Whether one was needed is settled
        upstream, since the principal names the identity, not how it authenticated: a Connection's
        bearer resolves only through `ActionsOAuthProxy`, which attaches its grant.
        """
        async with self._sessions.begin() as session:
            if external_grant is not None and (
                principal != external_grant.principal() or not await self._grant_authorized(session, external_grant)
            ):
                raise ExternalGrantNotAuthorizedError("external grant is not authorized")
            now = datetime.now(UTC)
            inserted_id = await session.scalar(
                pg_insert(ActionRequestRow)
                .values(
                    id=request_id,
                    idempotency_key=body.idempotency_key,
                    action=body.action,
                    arguments=body.arguments,
                    title=body.title,
                    description=body.description,
                    caller_namespace=principal.account.namespace,
                    caller_name=principal.account.name,
                    external_grant=external_grant,
                    state=ActionState.DECISION_PENDING,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["caller_namespace", "caller_name", "idempotency_key"])
                .returning(ActionRequestRow.id)
            )
            if inserted_id is None:
                raise ActionConflictError(
                    "idempotency key already used by this caller; read that request by key instead of resubmitting"
                )
            row = await session.get(ActionRequestRow, inserted_id)
            if row is None:
                raise RuntimeError("inserted ActionRequest is unreadable")
            _record_event(session, row, now)
            if vote is not None:
                _record_decision(
                    session,
                    row,
                    now,
                    verdict=Verdict.ALLOW if vote.outcome.verdict is ProviderVerdict.ALLOW else Verdict.DENY,
                    provider=vote.provider,
                    operator=None,
                    idempotency_key=f"auto:{row.id}",
                    reason_code=vote.outcome.reason_code,
                    reason_description=vote.outcome.reason_description,
                    policy_evidence=vote.outcome.evidence,
                )
                await session.flush()
            return await self._view(session, row, principal)

    async def list_requests(
        self, principal: ReadPrincipal, *, states: Sequence[ActionState] = (), idempotency_key: str | None = None
    ) -> list[ActionRequestView]:
        query = select(ActionRequestRow).order_by(ActionRequestRow.created_at.desc())
        if isinstance(principal, CallerPrincipal):
            query = query.where(
                ActionRequestRow.caller_namespace == principal.account.namespace,
                ActionRequestRow.caller_name == principal.account.name,
            )
        if states:
            query = query.where(ActionRequestRow.state.in_(states))
        if idempotency_key is not None:
            query = query.where(ActionRequestRow.idempotency_key == idempotency_key)
        async with self._sessions() as session:
            rows = list(await session.scalars(query))
            return [await self._view(session, row, principal) for row in rows]

    async def history(
        self, principal: OperatorPrincipal, *, limit: int, cursor: str | None
    ) -> tuple[list[ActionRequestView], str | None]:
        query = select(ActionRequestRow).where(ActionRequestRow.history_at.is_not(None))
        if cursor is not None:
            if len(cursor) > 1024:
                raise ValueError("invalid history cursor")
            try:
                timestamp, identifier = json.loads(urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                before = (datetime.fromisoformat(timestamp), UUID(identifier))
                if before[0].tzinfo is None:
                    raise ValueError("naive cursor")
            except (ValueError, TypeError, UnicodeDecodeError, Base64Error) as error:
                raise ValueError("invalid history cursor") from error
            query = query.where(tuple_(ActionRequestRow.history_at, ActionRequestRow.id) < before)
        query = query.order_by(ActionRequestRow.history_at.desc(), ActionRequestRow.id.desc()).limit(limit + 1)
        async with self._sessions() as session:
            rows = list(await session.scalars(query))
            page = rows[:limit]
            last = page[-1] if len(rows) > limit else None
            next_cursor = (
                urlsafe_b64encode(json.dumps([last.history_at.isoformat(), str(last.id)]).encode()).decode().rstrip("=")
                if last is not None and last.history_at is not None
                else None
            )
            return [await self._view(session, row, principal) for row in page], next_cursor

    async def get(self, request_id: UUID, principal: ReadPrincipal) -> ActionRequestView:
        async with self._sessions() as session:
            row = await session.get(ActionRequestRow, request_id)
            if row is None or not _may_read(row, principal):
                raise ActionNotFoundError(str(request_id))
            return await self._view(session, row, principal)

    async def events(
        self, request_id: UUID, principal: ReadPrincipal, *, after_sequence: int = 0, limit: int | None = None
    ) -> list[ActionEventView]:
        """`after_sequence` is the last sequence the caller already has; polling it back is a no-op."""
        async with self._sessions() as session:
            row = await session.get(ActionRequestRow, request_id)
            if row is None or not _may_read(row, principal):
                raise ActionNotFoundError(str(request_id))
            events = list(
                await session.scalars(
                    select(ActionEventRow)
                    .where(ActionEventRow.request_id == request_id, ActionEventRow.sequence > after_sequence)
                    .order_by(ActionEventRow.sequence)
                    .limit(limit)
                )
            )
            return [
                ActionEventView(
                    sequence=e.sequence,
                    state=e.state,
                    at=e.at,
                    actor=None if e.actor is None else TypeAdapter(Principal).validate_python(e.actor),
                )
                for e in events
            ]

    async def cancel(self, request_id: UUID, principal: Principal) -> CancellationResult:
        """Withdraw only before the atomic dispatch claim, without interrupting an executor."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            # Operator-all read/decision authority does not confer cancellation ownership.
            if row is None or not _is_caller(row, principal):
                raise ActionNotFoundError(str(request_id))
            state = row.state
            if state is ActionState.CANCELLED:
                outcome = CancellationOutcome.ALREADY_CANCELLED
            elif state in {ActionState.DENIED, ActionState.SUCCEEDED, ActionState.FAILED}:
                outcome = CancellationOutcome.ALREADY_FINISHED
            elif state in {ActionState.DISPATCHING, ActionState.RUNNING, ActionState.EXECUTION_UNKNOWN}:
                outcome = CancellationOutcome.TOO_LATE
            else:
                now = datetime.now(UTC)
                if state is ActionState.ALLOWED:
                    execution = await session.scalar(
                        select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
                    )
                    if execution is None or execution.state != ExecutionState.PENDING_DISPATCH:
                        raise ActionConflictError("allowed request has no pending dispatch")
                    execution.state = ExecutionState.CANCELLED
                    execution.completed_at = now
                row.state = ActionState.CANCELLED
                row.version += 1
                row.updated_at = now
                _record_event(session, row, now, actor=principal)
                outcome = CancellationOutcome.CANCELLED
            return CancellationResult(outcome=outcome, request=await self._view(session, row, principal))

    async def decide(
        self, request_id: UUID, body: DecisionInput, principal: OperatorPrincipal, *, provider: str
    ) -> tuple[ActionRequestView, bool]:
        """Human/operator Decision route; only an operator has one to make."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            if row is None:
                raise ActionNotFoundError(str(request_id))
            prior_key = await session.scalar(
                select(DecisionRow).where(
                    DecisionRow.provider == provider,
                    DecisionRow.operator_issuer == principal.issuer,
                    DecisionRow.operator_subject == principal.subject,
                    DecisionRow.idempotency_key == body.idempotency_key,
                )
            )
            if prior_key is not None:
                if prior_key.request_id != request_id or prior_key.verdict != body.verdict:
                    raise ActionConflictError("decision idempotency key was already used for another decision")
                return await self._view(session, row, principal), False
            if row.version != body.expected_version:
                raise ActionConflictError(
                    f"request changed: expected version {body.expected_version}, current version {row.version}"
                )
            if row.state != ActionState.DECISION_PENDING:
                raise ActionConflictError(f"request was already decided; current state is {row.state}")
            should_dispatch = _record_decision(
                session,
                row,
                datetime.now(UTC),
                verdict=body.verdict,
                provider=provider,
                operator=principal,
                idempotency_key=body.idempotency_key,
                decision_note=body.decision_note,
            )
            await session.flush()
            return await self._view(session, row, principal), should_dispatch

    async def pending_dispatches(self) -> list[UUID]:
        async with self._sessions() as session:
            return list(
                await session.scalars(
                    select(ExecutionRow.request_id).where(ExecutionRow.state == ExecutionState.PENDING_DISPATCH)
                )
            )

    async def claim_execution(
        self,
        request_id: UUID,
        *,
        executor_id: str,
        lease_duration: timedelta,
        can_dispatch: Callable[[ActionIdentity], bool] | None = None,
    ) -> ExecutionClaim | None:
        """Atomically reserve the only execution and grant its first lease window."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(str(request_id))
            if execution.state != ExecutionState.PENDING_DISPATCH:
                return None
            if row.state != ActionState.ALLOWED:
                raise ActionConflictError(f"cannot dispatch request in {row.state}")
            now = datetime.now(UTC)
            if row.external_grant is not None and not await self._grant_authorized(session, row.external_grant):
                # Preserve the historical Decision; the original grant no longer permits dispatch.
                execution.state = ExecutionState.FAILED
                execution.error = {"code": "external_grant_not_authorized"}
                execution.completed_at = now
                row.state = ActionState.FAILED
                row.version += 1
                row.updated_at = now
                _record_event(session, row, now)
                return None
            # Authority is checked even during outages. Replica-local availability is not stored.
            if can_dispatch is not None and not can_dispatch(row.action):
                return None
            lease_token = uuid4()
            lease_expires_at = now + lease_duration
            execution.state = ExecutionState.DISPATCHING
            execution.executor_id = executor_id
            execution.lease_token = lease_token
            execution.lease_expires_at = lease_expires_at
            execution.heartbeat_at = now
            row.state = ActionState.DISPATCHING
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)
            return ExecutionClaim(
                request_id=request_id,
                executor_id=executor_id,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )

    async def heartbeat_execution(
        self, request_id: UUID, executor_id: str, lease_token: UUID, *, lease_duration: timedelta
    ) -> bool:
        """Renew the lease. False means the caller is not (or no longer) the recognized owner."""
        async with self._sessions.begin() as session:
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if execution is None or not _owns_lease(execution, executor_id, lease_token):
                return False
            if execution.state not in {ExecutionState.DISPATCHING, ExecutionState.RUNNING}:
                return False
            now = datetime.now(UTC)
            execution.lease_expires_at = now + lease_duration
            execution.heartbeat_at = now
            return True

    async def mark_running(self, request_id: UUID) -> ExecutionRequest:
        """Cross the no-replay boundary immediately before invoking the executor."""
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(str(request_id))
            if row.state != ActionState.DISPATCHING or execution.state != ExecutionState.DISPATCHING:
                raise ActionConflictError(f"cannot start execution in {row.state}/{execution.state}")
            now = datetime.now(UTC)
            execution.state = ExecutionState.RUNNING
            execution.started_at = now
            row.state = ActionState.RUNNING
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)
            return ExecutionRequest(
                request_id=row.id,
                action=row.action,
                arguments=row.arguments,
                caller=ServiceAccountRef(namespace=row.caller_namespace, name=row.caller_name),
                external_grant=row.external_grant,
            )

    async def finish_execution(
        self, request_id: UUID, executor_id: str, lease_token: UUID, result: ExecutionResult
    ) -> None:
        """Deliver a terminal outcome authenticated by the lease granted at claim time.

        A late completion arriving after the lease already expired to `execution_unknown`
        is accepted as a reconciliation, still gated on presenting the same lease — it
        never starts a second effect, only records the truth about the one attempt made.
        """
        if result.state not in _TERMINAL_ACTION_STATE:
            raise ValueError(f"executor returned non-terminal state {result.state}")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ActionRequestRow).where(ActionRequestRow.id == request_id).with_for_update()
            )
            execution = await session.scalar(
                select(ExecutionRow).where(ExecutionRow.request_id == request_id).with_for_update()
            )
            if row is None or execution is None:
                raise ActionNotFoundError(str(request_id))
            if not _owns_lease(execution, executor_id, lease_token):
                raise ActionConflictError("caller is not the recognized owner of this execution's lease")
            now = datetime.now(UTC)
            if execution.state in {ExecutionState.DISPATCHING, ExecutionState.RUNNING}:
                execution.completed_at = now
            elif execution.state == ExecutionState.EXECUTION_UNKNOWN:
                execution.reconciled_at = now
                execution.reconciliation_source = ReconciliationSource.LATE_COMPLETION
                execution.reconciled_by = executor_id
            else:
                raise ActionConflictError(f"cannot finish execution in {execution.state}")
            execution.state = result.state
            execution.result = result.result
            execution.error = result.error
            row.state = _TERMINAL_ACTION_STATE[result.state]
            row.version += 1
            row.updated_at = now
            _record_event(session, row, now)

    async def expire_stale_leases(self, *, executor_health_timeout: timedelta) -> list[UUID]:
        """Mark executions whose lease lapsed unknown; never replay them.

        Distinguishes `executor_lost` (the owning executor's own health heartbeat is also
        stale) from `lease_expired` (the executor is otherwise heartbeating, but this one
        attempt stopped renewing) for operator diagnosis; both are equally final.
        """
        now = datetime.now(UTC)
        executor_stale_before = now - executor_health_timeout
        expired: list[UUID] = []
        async with self._sessions.begin() as session:
            executions = list(
                await session.scalars(
                    select(ExecutionRow)
                    .where(
                        ExecutionRow.state.in_([ExecutionState.DISPATCHING, ExecutionState.RUNNING]),
                        ExecutionRow.lease_expires_at < now,
                    )
                    .with_for_update()
                )
            )
            for execution in executions:
                row = await session.scalar(
                    select(ActionRequestRow).where(ActionRequestRow.id == execution.request_id).with_for_update()
                )
                if row is None:
                    continue
                executor_heartbeat = await session.get(ExecutorHeartbeatRow, execution.executor_id)
                reason = (
                    UnknownOutcomeReason.EXECUTOR_LOST
                    if executor_heartbeat is None or executor_heartbeat.heartbeat_at < executor_stale_before
                    else UnknownOutcomeReason.LEASE_EXPIRED
                )
                execution.state = ExecutionState.EXECUTION_UNKNOWN
                execution.error = {"kind": reason, "message": "dispatch outcome unknown; not replayed"}
                execution.completed_at = now
                row.state = ActionState.EXECUTION_UNKNOWN
                row.version += 1
                row.updated_at = now
                _record_event(session, row, now)
                expired.append(execution.request_id)
        return expired

    async def record_executor_heartbeat(self, executor_id: str) -> None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await session.execute(
                pg_insert(ExecutorHeartbeatRow)
                .values(executor_id=executor_id, started_at=now, heartbeat_at=now)
                .on_conflict_do_update(index_elements=["executor_id"], set_={"heartbeat_at": now})
            )

    async def _grant_authorized(self, session: AsyncSession, grant: ExternalGrantProvenance) -> bool:
        return await self._external_grants.authorize_action(session, grant)

    async def _view(self, session: AsyncSession, row: ActionRequestRow, principal: ReadPrincipal) -> ActionRequestView:
        decision = await session.scalar(select(DecisionRow).where(DecisionRow.request_id == row.id))
        execution = await session.scalar(select(ExecutionRow).where(ExecutionRow.request_id == row.id))
        operator = isinstance(principal, OperatorPrincipal)
        return ActionRequestView(
            id=row.id,
            idempotency_key=row.idempotency_key,
            action=row.action,
            arguments=row.arguments if operator else _redact(row.arguments),
            title=row.title,
            description=row.description,
            caller=ServiceAccountRef(namespace=row.caller_namespace, name=row.caller_name)
            if isinstance(principal, (OperatorPrincipal, ServiceReaderPrincipal))
            else None,
            external_grant=row.external_grant,
            state=row.state,
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            decision=_decision_view(decision),
            execution=_execution_view(execution),
        )


def _is_caller(row: ActionRequestRow, principal: ReadPrincipal) -> bool:
    return isinstance(principal, CallerPrincipal) and (row.caller_namespace, row.caller_name) == (
        principal.account.namespace,
        principal.account.name,
    )


def _may_read(row: ActionRequestRow, principal: ReadPrincipal) -> bool:
    return isinstance(principal, (OperatorPrincipal, ServiceReaderPrincipal)) or _is_caller(row, principal)


def _record_event(
    session: AsyncSession, row: ActionRequestRow, at: datetime, *, actor: Principal | None = None
) -> None:
    if row.state != ActionState.DECISION_PENDING and row.history_at is None:
        row.history_at = at
    session.add(
        ActionEventRow(
            request_id=row.id,
            sequence=row.version,
            state=row.state,
            at=at,
            actor=None if actor is None else actor.model_dump(mode="json"),
        )
    )


def _record_decision(
    session: AsyncSession,
    row: ActionRequestRow,
    now: datetime,
    *,
    verdict: Verdict,
    provider: str,
    operator: OperatorPrincipal | None,
    idempotency_key: str,
    decision_note: str | None = None,
    reason_code: str | None = None,
    reason_description: str | None = None,
    policy_evidence: PolicyEvidence | None = None,
) -> bool:
    """Decide a pending `row` in the caller's transaction; returns whether it now awaits dispatch."""
    session.add(
        DecisionRow(
            request_id=row.id,
            verdict=verdict,
            provider=provider,
            operator_issuer=None if operator is None else operator.issuer,
            operator_subject=None if operator is None else operator.subject,
            decision_note=decision_note,
            reason_code=reason_code,
            reason_description=reason_description,
            policy_evidence=policy_evidence,
            idempotency_key=idempotency_key,
            decided_at=now,
        )
    )
    row.state = ActionState.ALLOWED if verdict is Verdict.ALLOW else ActionState.DENIED
    row.version += 1
    row.updated_at = now
    _record_event(session, row, now)
    should_dispatch = verdict is Verdict.ALLOW
    if should_dispatch:
        session.add(ExecutionRow(request_id=row.id, state=ExecutionState.PENDING_DISPATCH, created_at=now))
    return should_dispatch


def _decision_view(row: DecisionRow | None) -> DecisionView | None:
    if row is None:
        return None
    return DecisionView(
        id=row.id,
        verdict=row.verdict,
        provider=row.provider,
        operator=operator_or_none(row.operator_issuer, row.operator_subject),
        decision_note=row.decision_note,
        reason_code=row.reason_code,
        reason_description=row.reason_description,
        policy_evidence=row.policy_evidence,
        idempotency_key=row.idempotency_key,
        decided_at=row.decided_at,
    )


def _execution_view(row: ExecutionRow | None) -> ExecutionView | None:
    if row is None:
        return None
    return ExecutionView(
        id=row.id,
        state=row.state,
        result=_redact_value(row.result),
        error=_redact(row.error) if row.error is not None else None,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        reconciled_at=row.reconciled_at,
    )


def _owns_lease(execution: ExecutionRow, executor_id: str, lease_token: UUID) -> bool:
    return execution.executor_id == executor_id and execution.lease_token == lease_token


def _redact(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: "[redacted]" if _secret_key(key) else _redact_value(item) for key, item in value.items()}


def _secret_key(key: str) -> bool:
    return key.lower().replace("_", "").replace("-", "") in _SECRET_KEYS


def _redact_value(value: JsonValue | None) -> JsonValue | None:
    if isinstance(value, dict):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value
