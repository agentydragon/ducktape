"""Bounded, notification-driven receipt reads. Ending a wait never cancels an Action."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from x.agentplane.action_service.models import ActionRequestView, ActionState, Principal
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates


class WaitUntil(StrEnum):
    DECISION = "decision"
    TERMINAL = "terminal"


class WaitOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_seconds: float = Field(default=0, ge=0, le=30, allow_inf_nan=False)
    wait_until: WaitUntil = WaitUntil.TERMINAL


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
    def __init__(self, service: ActionService, updates: ActionUpdates) -> None:
        self._service = service
        self._updates = updates

    async def get(self, request_id: UUID, principal: Principal, options: WaitOptions) -> ActionRequestView:
        if options.wait_seconds == 0:
            return await self._service.get(request_id, principal)
        # Authorize before allocating a subscription, then re-read AFTER subscribing so a
        # commit racing setup cannot be missed. Each read uses the canonical owner projection.
        initial = await self._service.get(request_id, principal)
        if satisfied(initial, options.wait_until):
            return initial
        with self._updates.subscribe(request_id) as changed:
            deadline = asyncio.get_running_loop().time() + options.wait_seconds
            while True:
                changed.clear()
                self._updates.check_available()
                view = await self._service.get(request_id, principal)
                if satisfied(view, options.wait_until):
                    return view
                try:
                    async with asyncio.timeout_at(deadline):
                        await changed.wait()
                except TimeoutError:
                    self._updates.check_available()
                    return await self._service.get(request_id, principal)
