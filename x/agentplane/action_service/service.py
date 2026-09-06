"""ActionRequest coordinator: human DecisionProvider and one single-shot executor dispatch."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from x.agentplane.action_service.db import ActionStore
from x.agentplane.action_service.models import (
    ActionEventView,
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    DecisionInput,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Executor,
    NotificationOutbox,
    NullNotificationOutbox,
    Principal,
)

logger = logging.getLogger(__name__)


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

    def __init__(self, store: ActionStore, executor: Executor, *, outbox: NotificationOutbox | None = None) -> None:
        self._store = store
        self._executor = executor
        self._outbox = outbox or NullNotificationOutbox()
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
        if created:
            await self._outbox.wake()
        return view

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
        error = task.exception()
        if error is not None:
            logger.exception("action dispatch task failed", exc_info=error)

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
