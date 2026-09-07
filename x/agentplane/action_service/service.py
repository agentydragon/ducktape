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
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from x.agentplane.action_service.db import ActionConflictError, ActionStore
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionContext,
    DecisionInput,
    DecisionProvider,
    ExecutionClaim,
    ExecutionLease,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Executor,
    Principal,
    ProviderOutcome,
    ProviderVerdict,
    UnknownOutcomeReason,
    Verdict,
)

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
PROVIDER_TIMEOUT_REASON = "provider_timeout"
PROVIDER_UNAVAILABLE_REASON = "provider_unavailable"

DEFAULT_LEASE_DURATION = timedelta(seconds=30)
DEFAULT_LEASE_SWEEP_INTERVAL = timedelta(seconds=5)
DEFAULT_EXECUTOR_HEARTBEAT_INTERVAL = timedelta(seconds=10)
DEFAULT_EXECUTOR_HEALTH_TIMEOUT = timedelta(seconds=45)


@dataclass(frozen=True)
class _ProviderVote:
    provider: str
    outcome: ProviderOutcome


class ExecutionOutcomeUnknownError(Exception):
    """The adapter cannot prove whether the external effect started, so replay is forbidden."""


class EchoExecutor:
    """Explicit v0 fixture adapter proving the service seam without claiming an MCP integration."""

    CAPABILITY = "agentplane:v0.echo"

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({self.CAPABILITY})

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": request.arguments})


class _StoreBackedLease:
    """The seam a future out-of-process worker would present over the wire, called in-process for v0."""

    def __init__(self, store: ActionStore, claim: ExecutionClaim, lease_duration: timedelta) -> None:
        self._store = store
        self._claim = claim
        self._lease_duration = lease_duration

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
        executor: Executor,
        *,
        providers: Sequence[DecisionProvider] = (),
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        executor_id: str | None = None,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        lease_sweep_interval: timedelta = DEFAULT_LEASE_SWEEP_INTERVAL,
        executor_heartbeat_interval: timedelta = DEFAULT_EXECUTOR_HEARTBEAT_INTERVAL,
        executor_health_timeout: timedelta = DEFAULT_EXECUTOR_HEALTH_TIMEOUT,
    ) -> None:
        self._store = store
        self._executor = executor
        self._providers = tuple(providers)
        self._provider_timeout_seconds = provider_timeout_seconds
        self._executor_id = executor_id or f"executor-{uuid4()}"
        self._lease_duration = lease_duration
        self._lease_sweep_interval = lease_sweep_interval
        self._executor_heartbeat_interval = executor_heartbeat_interval
        self._executor_health_timeout = executor_health_timeout
        self._tasks: set[asyncio.Task[None]] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Resume only dispatches that provably never started; liveness sweeps handle the rest.

        A restart never assumes in-flight work died with the old process: the sweep loop
        applies the same bounded-lease-expiry rule regardless of which process is running it,
        so a hard kill and a live separate executor are indistinguishable here by design.
        """
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="action-executor-heartbeat")
        self._sweep_task = asyncio.create_task(self._sweep_loop(), name="action-lease-sweep")
        for request_id in await self._store.pending_dispatches():
            self._schedule(request_id)

    async def close(self) -> None:
        background = [task for task in (self._heartbeat_task, self._sweep_task) if task is not None]
        for task in [*self._tasks, *background]:
            task.cancel()
        await asyncio.gather(*self._tasks, *background, return_exceptions=True)
        self._tasks.clear()
        self._heartbeat_task = None
        self._sweep_task = None

    async def submit(self, body: ActionRequestInput, principal: Principal) -> ActionRequestView:
        view, created = await self._store.submit(body, principal, supported_capabilities=self._executor.capabilities)
        if not created:
            return view
        return await self._auto_decide(view, body, principal)

    async def _auto_decide(
        self, view: ActionRequestView, body: ActionRequestInput, principal: Principal
    ) -> ActionRequestView:
        """Evaluate configured synchronous providers; defer to the human path on no decisive outcome."""
        if not self._providers:
            return view
        context = DecisionContext(
            request_id=view.id, capability=body.capability, arguments=body.arguments, caller_principal=principal
        )
        vote = await self._evaluate_providers(context)
        if vote is None:
            return view
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
            )
        except ActionConflictError:
            # A human operator's Decision committed first (a genuine race); the auto-provider
            # outcome is stale and must never override or duplicate the winning Decision.
            logger.info("auto-provider decision for %s was stale; another Decision already won", view.id)
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

    async def list_requests(
        self, principal: Principal, *, states: tuple[ActionState, ...] = ()
    ) -> list[ActionRequestView]:
        return await self._store.list_requests(principal, states=states)

    async def get(self, request_id: UUID, principal: Principal) -> ActionRequestView:
        return await self._store.get(request_id, principal)

    async def events(self, request_id: UUID, principal: Principal, *, after_sequence: int = 0) -> list[ActionEventView]:
        return await self._store.events(request_id, principal, after_sequence=after_sequence)

    async def decide(self, request_id: UUID, body: DecisionInput, principal: Principal) -> ActionRequestView:
        view, should_dispatch = await self._store.decide(request_id, body, principal, provider=self.HUMAN_PROVIDER)
        if should_dispatch:
            self._schedule(request_id)
        return view

    def _schedule(self, request_id: UUID) -> None:
        task = asyncio.create_task(self._dispatch_once(request_id), name=f"action-dispatch-{request_id}")
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        if task.exception() is not None:
            # Raw adapter/database exceptions can contain request or provider material. The durable
            # state machine carries the safe classification; logs record only that coordination failed.
            logger.error("action dispatch coordination failed; request will not be retried")

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

    async def _dispatch_once(self, request_id: UUID) -> None:
        claim = await self._store.claim_execution(
            request_id, executor_id=self._executor_id, lease_duration=self._lease_duration
        )
        if claim is None:
            return
        lease = _StoreBackedLease(self._store, claim, self._lease_duration)
        try:
            request = await self._store.mark_running(request_id)
            result = await self._executor.execute(request, lease)
        except ExecutionOutcomeUnknownError:
            # Adapter exception text can contain provider responses or credentials. Persist and
            # return only the stable classification; the service never projects raw exceptions.
            result = ExecutionResult(
                state=ExecutionState.EXECUTION_UNKNOWN,
                error={
                    "kind": UnknownOutcomeReason.ADAPTER_OUTCOME_UNKNOWN,
                    "message": "execution outcome is unknown; not replayed",
                },
            )
        except asyncio.CancelledError:
            # A graceful stop can record uncertainty immediately rather than waiting on the
            # lease to lapse. A hard process loss leaves no code running to reach this branch;
            # the lease sweep makes the same transition instead, on its own schedule.
            await asyncio.shield(
                self._store.finish_execution(
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
            )
            raise
        except Exception as error:  # a failed executor call is terminal; this request is never retried
            # Do not retain exception text: provider SDK errors routinely echo Authorization or
            # request material. The exception class is enough to diagnose the adapter category.
            result = ExecutionResult(
                state=ExecutionState.FAILED,
                error={"kind": type(error).__name__, "message": "executor failed; see credential-safe adapter metrics"},
            )
        await self._store.finish_execution(request_id, claim.executor_id, claim.lease_token, result)
