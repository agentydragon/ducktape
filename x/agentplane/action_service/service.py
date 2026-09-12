"""ActionRequest coordinator: DecisionProvider aggregation, human fallback, and single-shot dispatch.

Executor liveness: the coordinator holds one `executor_id` for its own process lifetime and
sends it an executor-level health heartbeat regardless of whether it currently owns any
Execution. Each claimed Execution additionally gets its own unguessable `lease_token` and a
bounded lease; the adapter renews it via `ExecutionLease.heartbeat()` while working, and a
periodic sweep marks any Execution whose lease lapsed `execution_unknown` — whether that is
because the executor died or because this coordinator process itself was killed. Neither
death is distinguishable from the other from the database's point of view, and both get the
same safe treatment: never replay, let a later authenticated completion or authoritative
status lookup reconcile the one attempt that was made.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jsonschema

from x.agentplane.action_service.catalog import ActionCatalog, ActionIdentity, UnknownActionError
from x.agentplane.action_service.db import ActionConflictError, ActionStore
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CancellationResult,
    DecisionInput,
    ExecutionClaim,
    ExecutionResult,
    ExecutionState,
    Executor,
    ExternalGrantProvenance,
    Principal,
    ProviderOutcome,
    ProviderVerdict,
    SandboxCaller,
    ServiceAccountCaller,
    UnknownOutcomeReason,
    Verdict,
)
from x.agentplane.action_service.policy_evaluation import resolve_bindings
from x.agentplane.action_service.policy_informer import PolicyIndex
from x.agentplane.action_service.policy_view import (
    CallerActionPolicyView,
    PolicySubject,
    SubjectActionPolicyView,
    caller_view,
    subject_view,
)
from x.agentplane.action_service.providers import DecisionContext, DecisionProvider

# types-jsonschema stubs import referencing; the mypy aspect needs that typed package directly.
# gazelle:include_dep @pypi//referencing

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
PROVIDER_TIMEOUT_REASON = "provider_timeout"
PROVIDER_UNAVAILABLE_REASON = "provider_unavailable"

DEFAULT_LEASE_DURATION = timedelta(seconds=30)
DEFAULT_LEASE_SWEEP_INTERVAL = timedelta(seconds=5)
DEFAULT_EXECUTOR_HEARTBEAT_INTERVAL = timedelta(seconds=10)
DEFAULT_EXECUTOR_HEALTH_TIMEOUT = timedelta(seconds=45)
DEFAULT_DISPATCH_POLL_INTERVAL = timedelta(seconds=1)
DEFAULT_DRAIN_TIMEOUT = timedelta(seconds=20)
DEFAULT_STOP_TIMEOUT = timedelta(seconds=5)


@dataclass(frozen=True)
class _ProviderVote:
    provider: str
    outcome: ProviderOutcome


class ExecutionOutcomeUnknownError(Exception):
    """The adapter cannot prove whether the external effect started, so replay is forbidden."""


class ServiceDrainingError(Exception):
    """This replica no longer accepts new work."""


class UnsupportedActionError(Exception):
    """The requested group/action is unknown, unavailable, or has no executor binding."""


class InvalidActionArgumentsError(Exception):
    """Arguments do not match the advertised Action schema; nothing was persisted."""


def _caller(
    principal: Principal, external_grant: ExternalGrantProvenance | None
) -> SandboxCaller | ServiceAccountCaller:
    """The typed caller providers see: the grant's ServiceAccount, else the Sandbox the principal
    was minted for. Admission already refused any grant a ServiceAccount does not back."""
    if external_grant is None:
        return SandboxCaller.from_principal(principal)
    return ServiceAccountCaller(service_account=external_grant.caller, grant_revision=external_grant.revision)


class _StoreBackedLease:
    """The seam a future out-of-process worker would present over the wire, called in-process for v0."""

    def __init__(self, store: ActionStore, claim: ExecutionClaim, lease_duration: timedelta) -> None:
        self._store = store
        self._claim = claim
        self._lease_duration = lease_duration

    @property
    def renewal_interval(self) -> timedelta:
        return self._lease_duration / 3

    async def heartbeat(self) -> bool:
        return await self._store.heartbeat_execution(
            self._claim.request_id,
            self._claim.executor_id,
            self._claim.lease_token,
            lease_duration=self._lease_duration,
        )


class ActionService:
    HUMAN_PROVIDER = "human_operator"

    def __init__(
        self,
        store: ActionStore,
        catalog: ActionCatalog,
        executors: Mapping[str, Executor],
        *,
        providers: Sequence[DecisionProvider] = (),
        policies: PolicyIndex | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        executor_id: str | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        lease_sweep_interval: timedelta = DEFAULT_LEASE_SWEEP_INTERVAL,
        executor_heartbeat_interval: timedelta = DEFAULT_EXECUTOR_HEARTBEAT_INTERVAL,
        executor_health_timeout: timedelta = DEFAULT_EXECUTOR_HEALTH_TIMEOUT,
        dispatch_poll_interval: timedelta = DEFAULT_DISPATCH_POLL_INTERVAL,
        drain_timeout: timedelta = DEFAULT_DRAIN_TIMEOUT,
        on_drain: Callable[[], None] | None = None,
        stop_timeout: timedelta = DEFAULT_STOP_TIMEOUT,
    ) -> None:
        if lease_duration <= timedelta(0) or drain_timeout < timedelta(0) or stop_timeout <= timedelta(0):
            raise ValueError("lease/stop timeouts must be positive and drain timeout nonnegative")
        self._on_drain = on_drain
        self._drain_timeout = drain_timeout
        self._stop_timeout = stop_timeout
        self._drain_deadline: float | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._store = store
        self._catalog = catalog
        self._executors = dict(executors)
        self._providers = tuple(providers)
        # None: this deployment watches no policy objects. An index nothing feeds never syncs, so
        # every caller is human-only and reads as much.
        self._policies = policies if policies is not None else PolicyIndex()
        self._clock = clock
        self._provider_timeout_seconds = provider_timeout_seconds
        self._executor_id = executor_id or f"executor-{uuid4()}"
        self._lease_duration = lease_duration
        self._lease_sweep_interval = lease_sweep_interval
        self._executor_heartbeat_interval = executor_heartbeat_interval
        self._executor_health_timeout = executor_health_timeout
        self._dispatch_poll_interval = dispatch_poll_interval
        self._tasks: set[asyncio.Task[None]] = set()
        self._scheduled: set[UUID] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._dispatch_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Resume only dispatches that provably never started; liveness sweeps handle the rest.

        A restart never assumes in-flight work died with the old process: the sweep loop
        applies the same bounded-lease-expiry rule regardless of which process is running it,
        so a hard kill and a live separate executor are indistinguishable here by design.
        """
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="action-executor-heartbeat")
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="action-lease-sweep")
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="action-dispatch-recovery")
        for request_id in await self._store.pending_dispatches():
            self._schedule(request_id)

    @property
    def draining(self) -> bool:
        return self._drain_deadline is not None

    def begin_drain(self) -> None:
        # Synchronous fence: queued dispatches must observe this before entering claim_execution.
        # Claims already awaiting the database remain in _tasks and belong to the drain.
        if self._drain_deadline is None:
            self._drain_deadline = asyncio.get_running_loop().time() + self._drain_timeout.total_seconds()
            if self._on_drain is not None:
                self._on_drain()
            self._close_task = asyncio.create_task(self._close(), name="action-service-drain")

    async def close(self) -> None:
        self.begin_drain()
        assert self._close_task is not None
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        assert self._drain_deadline is not None
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            await asyncio.gather(self._dispatch_task, return_exceptions=True)
            self._dispatch_task = None
        tasks = set(self._tasks)
        if tasks:
            _, pending = await asyncio.wait(
                tasks, timeout=max(0, self._drain_deadline - asyncio.get_running_loop().time())
            )
            for task in pending:
                task.cancel()
            # Adapters and database stay open while cancellation records uncertainty. If
            # persistence is unavailable, bounded lease expiry supplies the same no-replay state.
            if pending:
                _, unfinished = await asyncio.wait(pending, timeout=self._stop_timeout.total_seconds())
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
        background = [task for task in (self._heartbeat_task, self._sweep_task) if task is not None]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        self._heartbeat_task = None
        self._sweep_task = None

    async def submit(
        self, body: ActionRequestInput, principal: Principal, *, external_grant: ExternalGrantProvenance | None = None
    ) -> ActionRequestView:
        if self.draining:
            raise ServiceDrainingError("Action Service is draining")
        self._resolve_executor(body.action)
        _, action = self._catalog.resolve(body.action.group, body.action.name)
        try:
            jsonschema.validate(body.arguments, action.input_schema)
        except jsonschema.ValidationError:
            raise InvalidActionArgumentsError("arguments do not match the advertised Action schema") from None
        view = await self._store.submit(body, principal, external_grant=external_grant)
        return await self._auto_decide(view, body, principal, external_grant)

    def _resolve_executor(self, identity: ActionIdentity) -> Executor:
        group_key, action_key = identity.group, identity.name
        try:
            group, _ = self._catalog.resolve(group_key, action_key)
        except UnknownActionError as error:
            raise UnsupportedActionError(identity) from error
        executor = self._executors.get(group_key)
        if not group.available or executor is None:
            raise UnsupportedActionError(identity)
        return executor

    async def _auto_decide(
        self,
        view: ActionRequestView,
        body: ActionRequestInput,
        principal: Principal,
        external_grant: ExternalGrantProvenance | None,
    ) -> ActionRequestView:
        """Evaluate configured synchronous providers once, against the policy objects as they stand
        now; defer to the human path on no decisive outcome."""
        if not self._providers:
            return view
        caller = _caller(principal, external_grant)
        context = DecisionContext(
            request_id=view.id,
            action=body.action,
            arguments=body.arguments,
            caller=caller,
            bindings=resolve_bindings(self._policies, caller, self._clock()),
        )
        vote = await self._evaluate_providers(context)
        if vote is None:
            return await self._store.get(view.id, principal)
        try:
            decided, should_dispatch = await self._store.decide_by_provider(
                view.id,
                principal,
                verdict=Verdict.ALLOW if vote.outcome.verdict is ProviderVerdict.ALLOW else Verdict.DENY,
                provider=vote.provider,
                idempotency_key=f"auto:{view.id}",
                expected_version=view.version,
                reason_code=vote.outcome.reason_code,
                reason_description=vote.outcome.reason_description,
                policy_evidence=vote.outcome.evidence,
            )
        except ActionConflictError:
            # A human Decision or caller cancellation may commit during provider evaluation.
            logger.info("auto-provider decision for %s was stale; another transition already won", view.id)
            return await self._store.get(view.id, principal)
        if should_dispatch:
            self._schedule(view.id)
        return decided

    async def _evaluate_providers(self, context: DecisionContext) -> _ProviderVote | None:
        """Run every configured provider to completion first, so deny dominance never depends on
        which provider happens to answer fastest."""
        votes = await asyncio.gather(*(self._ask(provider, context) for provider in self._providers))
        for vote in votes:
            if vote.outcome.verdict is ProviderVerdict.DENY:
                return vote
        for vote in votes:
            if vote.outcome.verdict is ProviderVerdict.ALLOW:
                return vote
        return None

    async def _ask(self, provider: DecisionProvider, context: DecisionContext) -> _ProviderVote:
        try:
            outcome = await asyncio.wait_for(provider.decide(context), timeout=self._provider_timeout_seconds)
        except TimeoutError:
            logger.warning("decision provider %s timed out; treating as no_opinion", provider.name)
            outcome = ProviderOutcome(verdict=ProviderVerdict.NO_OPINION, reason_code=PROVIDER_TIMEOUT_REASON)
        except Exception:
            # A provider's raw exception text can carry backend/credential material; never persist
            # or project it. Unavailability is not an allow — it defers like a silent no-opinion.
            logger.exception("decision provider %s raised; treating as no_opinion", provider.name)
            outcome = ProviderOutcome(verdict=ProviderVerdict.NO_OPINION, reason_code=PROVIDER_UNAVAILABLE_REASON)
        return _ProviderVote(provider=provider.name, outcome=outcome)

    def caller_action_policy(
        self, principal: Principal, external_grant: ExternalGrantProvenance | None
    ) -> CallerActionPolicyView:
        """What the caller's own bindings auto-decide, resolved as admission would resolve them now."""
        return caller_view(self._policies, _caller(principal, external_grant), self._clock())

    def target_action_policy(self, subject: PolicySubject) -> CallerActionPolicyView:
        """What a named subject's bindings auto-decide, in the view a caller may see."""
        return caller_view(self._policies, subject, self._clock())

    def subject_action_policy(self, subject: PolicySubject) -> SubjectActionPolicyView:
        """The operator's view of what a named subject's bindings auto-decide, resolved the same way."""
        return subject_view(self._policies, subject, self._clock())

    async def list_requests(
        self, principal: Principal, *, states: tuple[ActionState, ...] = (), idempotency_key: str | None = None
    ) -> list[ActionRequestView]:
        return await self._store.list_requests(principal, states=states, idempotency_key=idempotency_key)

    async def get(self, request_id: UUID, principal: Principal) -> ActionRequestView:
        return await self._store.get(request_id, principal)

    async def cancel(self, request_id: UUID, principal: Principal) -> CancellationResult:
        return await self._store.cancel(request_id, principal)

    async def events(
        self, request_id: UUID, principal: Principal, *, after_sequence: int = 0, limit: int | None = None
    ) -> list[ActionEventView]:
        return await self._store.events(request_id, principal, after_sequence=after_sequence, limit=limit)

    async def decide(self, request_id: UUID, body: DecisionInput, principal: Principal) -> ActionRequestView:
        if self.draining:
            raise ServiceDrainingError("Action Service is draining")
        view, should_dispatch = await self._store.decide(request_id, body, principal, provider=self.HUMAN_PROVIDER)
        if should_dispatch:
            self._schedule(request_id)
        return view

    def _schedule(self, request_id: UUID) -> None:
        if self.draining or request_id in self._scheduled:
            return
        self._scheduled.add(request_id)
        task = asyncio.create_task(self._dispatch_once(request_id), name=f"action-dispatch-{request_id}")
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        request_id = task.get_name().removeprefix("action-dispatch-")
        with contextlib.suppress(ValueError):
            self._scheduled.discard(UUID(request_id))
        if task.cancelled():
            return
        if task.exception() is not None:
            # Raw adapter/database exceptions can contain request or provider material. The durable
            # state machine carries the safe classification; logs record only that coordination failed.
            logger.error("action dispatch coordination failed; request will not be retried")

    async def _dispatch_loop(self) -> None:
        """Reconcile durable pending dispatches so any healthy replica can take over."""
        while True:
            try:
                for request_id in await self._store.pending_dispatches():
                    self._schedule(request_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("pending Action dispatch reconciliation failed; will retry", exc_info=True)
            await asyncio.sleep(self._dispatch_poll_interval.total_seconds())

    async def _heartbeat_loop(self) -> None:
        """Prove this executor identity is alive, independent of any Execution it may hold."""
        while True:
            try:
                await self._store.record_executor_heartbeat(self._executor_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("executor heartbeat failed; will retry", exc_info=True)
            await asyncio.sleep(self._executor_heartbeat_interval.total_seconds())

    async def _sweep_loop(self) -> None:
        """Bound how long a stalled lease can hide an ambiguous outcome, across restarts."""
        while True:
            try:
                expired = await self._store.expire_stale_leases(executor_health_timeout=self._executor_health_timeout)
                if expired:
                    logger.info("lease sweep marked %d execution(s) unknown", len(expired))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("lease sweep failed; will retry", exc_info=True)
            await asyncio.sleep(self._lease_sweep_interval.total_seconds())

    def _can_dispatch(self, identity: ActionIdentity) -> bool:
        if self.draining:
            return False
        group = self._catalog.groups.get(identity.group)
        # Removed groups/actions are terminal, not an outage to park indefinitely.
        return group is None or identity.group not in self._executors or group.available

    async def _dispatch_once(self, request_id: UUID) -> None:
        if self.draining:
            return
        claim = await self._store.claim_execution(
            request_id,
            executor_id=self._executor_id,
            lease_duration=self._lease_duration,
            can_dispatch=self._can_dispatch,
        )
        if claim is None:
            return
        try:
            await self._execute_claim(claim)
        except asyncio.CancelledError:
            # A cancelled claim RPC may have committed without returning its token: no adapter
            # is invoked in that case, and lease expiry resolves the stranded dispatch. Once
            # we have the claim, try to publish uncertainty before closing the database.
            await self._store.finish_execution(
                request_id,
                claim.executor_id,
                claim.lease_token,
                ExecutionResult(
                    state=ExecutionState.EXECUTION_UNKNOWN,
                    error={
                        "kind": UnknownOutcomeReason.COORDINATOR_STOPPED,
                        "message": "dispatch outcome unknown; not replayed",
                    },
                ),
            )
            raise

    async def _execute_claim(self, claim: ExecutionClaim) -> None:
        lease = _StoreBackedLease(self._store, claim, self._lease_duration)
        try:
            request = await self._store.mark_running(claim.request_id)
            result = await self._resolve_executor(request.action).execute(request, lease)
        except ExecutionOutcomeUnknownError:
            result = ExecutionResult(
                state=ExecutionState.EXECUTION_UNKNOWN,
                error={
                    "kind": UnknownOutcomeReason.ADAPTER_OUTCOME_UNKNOWN,
                    "message": "execution outcome is unknown; not replayed",
                },
            )
        except Exception as error:
            result = ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": type(error).__name__, "message": "executor failed; see credential-safe adapter metrics"},
            )
        completion = asyncio.create_task(
            self._store.finish_execution(claim.request_id, claim.executor_id, claim.lease_token, result),
            name="action-completion",
        )
        renewal = asyncio.create_task(self._renew_completion_lease(lease), name="action-completion-renewal")
        try:
            await completion
        finally:
            completion.cancel()
            renewal.cancel()
            await asyncio.gather(completion, renewal, return_exceptions=True)

    async def _renew_completion_lease(self, lease: _StoreBackedLease) -> None:
        # A known result remains worth delivering even when renewal fails: finish_execution
        # authenticates the original claim and supports late reconciliation, not replay.
        while True:
            await asyncio.sleep(lease.renewal_interval.total_seconds())
            try:
                async with asyncio.timeout(lease.renewal_interval.total_seconds()):
                    if not await lease.heartbeat():
                        return
            except Exception:
                logger.warning("completion lease renewal failed; authenticated outcome delivery still pending")
                return
