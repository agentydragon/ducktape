"""Bounded, notification-driven receipt reads. Ending a wait never cancels an Action."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentplane.action_service.models import ActionRequestView, ActionState, Principal
from agentplane.action_service.service import ActionService
from agentplane.action_service.updates import ActionUpdates

WaitSeconds = Annotated[
    float, Field(ge=0, allow_inf_nan=False, description="Capped by this instance's max_wait_seconds setting.")
]


class WaitUntil(StrEnum):
    DECISION = "decision"
    TERMINAL = "terminal"


class WaitOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_seconds: WaitSeconds = 0
    wait_until: WaitUntil = WaitUntil.TERMINAL


class WaitLimitExceededError(ValueError):
    """A caller requested a wait longer than this Action Service instance allows."""

    def __init__(self, max_wait_seconds: float) -> None:
        super().__init__(f"wait.wait_seconds must be at most {max_wait_seconds:g} seconds for this instance.")


def satisfied(view: ActionRequestView, until: WaitUntil) -> bool:
    if until is WaitUntil.DECISION:
        return view.state is not ActionState.DECISION_PENDING
    return view.state not in {
        ActionState.DECISION_PENDING,
        ActionState.ALLOWED,
        ActionState.DISPATCHING,
        ActionState.RUNNING,
    }


class ActionWaiter:
    def __init__(self, service: ActionService, updates: ActionUpdates, max_wait_seconds: float) -> None:
        self._service = service
        self._updates = updates
        self._max_wait_seconds = max_wait_seconds

    def validate(self, options: WaitOptions) -> None:
        if options.wait_seconds > self._max_wait_seconds:
            raise WaitLimitExceededError(self._max_wait_seconds)

    async def get(self, request_id: UUID, principal: Principal, options: WaitOptions) -> ActionRequestView:
        self.validate(options)
        if options.wait_seconds == 0:
            return await self._service.get(request_id, principal)
        # Authorize before allocating a subscription, then re-read AFTER subscribing so a
        # commit racing setup cannot be missed. Each read uses the canonical owner projection.
        initial = await self._service.get(request_id, principal)
        if satisfied(initial, options.wait_until):
            return initial
        with self._updates.subscribe(request_id) as subscription:
            changed = subscription.changed
            deadline = asyncio.get_running_loop().time() + options.wait_seconds
            while True:
                changed.clear()
                subscription.check_available()
                view = await self._service.get(request_id, principal)
                subscription.check_available()
                if satisfied(view, options.wait_until):
                    return view
                try:
                    async with asyncio.timeout_at(deadline):
                        await changed.wait()
                except TimeoutError:
                    subscription.check_available()
                    view = await self._service.get(request_id, principal)
                    subscription.check_available()
                    return view
