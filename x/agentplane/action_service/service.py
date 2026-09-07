"""ActionRequest coordinator: DecisionProvider aggregation, human fallback, and single-shot dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from x.agentplane.action_service.db import ActionConflictError, ActionStore
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionContext,
    DecisionInput,
    DecisionProvider,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Executor,
    NotificationOutbox,
    NullNotificationOutbox,
    Principal,
    ProviderOutcome,
    ProviderVerdict,
    Verdict,
)

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0
PROVIDER_TIMEOUT_REASON = "provider_timeout"
PROVIDER_UNAVAILABLE_REASON = "provider_unavailable"


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

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(state=ExecutionState.SUCCEEDED, result={"echo": request.arguments})


class ActionService:
    HUMAN_PROVIDER = "human_operator"

    def __init__(
        self,
        store: ActionStore,
        executor: Executor,
        *,
        outbox: NotificationOutbox | None = None,
        providers: Sequence[DecisionProvider] = (),
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._executor = executor
        self._outbox = outbox or NullNotificationOutbox()
        self._providers = tuple(providers)
        self._provider_timeout_seconds = provider_timeout_seconds
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> int:
        """Recover unsafe in-flight work, then resume only dispatches that provably never started."""
        recovered = await self._store.recover_unknown()
        for request_id in await self._store.pending_dispatches():
            self._schedule(request_id)
        return recovered

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def submit(self, body: ActionRequestInput, principal: Principal) -> ActionRequestView:
        view, created = await self._store.submit(body, principal, supported_capabilities=self._executor.capabilities)
        if not created:
            return view
        await self._outbox.wake()
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

    async def events(self, request_id: UUID, principal: Principal) -> list[ActionEventView]:
        return await self._store.events(request_id, principal)

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

    async def _dispatch_once(self, request_id: UUID) -> None:
        if not await self._store.claim_execution(request_id):
            return
        try:
            request = await self._store.mark_running(request_id)
            result = await self._executor.execute(request)
        except ExecutionOutcomeUnknownError:
            # Adapter exception text can contain provider responses or credentials. Persist and
            # return only the stable classification; the service never projects raw exceptions.
            result = ExecutionResult(
                state=ExecutionState.EXECUTION_UNKNOWN,
                error={"kind": "execution_outcome_unknown", "message": "execution outcome is unknown; not replayed"},
            )
        except asyncio.CancelledError:
            # A graceful stop can record uncertainty. A hard process loss leaves RUNNING behind;
            # start() makes the same transition before accepting traffic.
            await asyncio.shield(
                self._store.finish_execution(
                    request_id,
                    ExecutionResult(
                        state=ExecutionState.EXECUTION_UNKNOWN,
                        error={"kind": "coordinator_stopped", "message": "dispatch outcome unknown; not replayed"},
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
        await self._store.finish_execution(request_id, result)
