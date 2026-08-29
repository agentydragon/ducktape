"""Focused contracts for leader-owned sandbox inventory invalidations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from haku.console.notifications.console_events import ConsoleEventHub, SandboxSessionsChangedEvent
from haku.console.session.runtime import SessionService
from haku.console.session.sandbox_claims import SandboxClaims
from haku.console.session.sandbox_observer import SandboxSessionObserver

OPERATOR_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OPERATOR_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, list[object]]] = []
        self.published = asyncio.Event()

    async def broadcast(self, operator_id: UUID, events: list[object]) -> None:
        self.events.append((operator_id, events))
        self.published.set()


class _Service:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate_sandbox_observations(self) -> None:
        self.invalidations += 1


class _Claim:
    def __init__(self) -> None:
        self.changed = asyncio.Event()

    def watch_changes(self, stop: asyncio.Event) -> AsyncIterator[None]:
        async def changes() -> AsyncIterator[None]:
            while not stop.is_set():
                changed = asyncio.create_task(self.changed.wait())
                stopped = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait((changed, stopped), return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stopped in done:
                    return
                self.changed.clear()
                yield None

        return changes()


async def test_change_invalidates_cache_and_broadcasts_to_every_active_operator() -> None:
    service = _Service()
    hub = _Hub()
    claim = _Claim()
    observer = SandboxSessionObserver(
        cast(SessionService, service),
        (cast(SandboxClaims, claim),),
        cast(AsyncEngine, object()),
        cast(ConsoleEventHub, hub),
        _operator_ids,
    )
    stop = asyncio.Event()
    observing = asyncio.create_task(observer._observe(stop))
    try:
        claim.changed.set()
        await asyncio.wait_for(hub.published.wait(), timeout=1)
    finally:
        stop.set()
        await observing

    assert service.invalidations == 1
    assert {operator_id for operator_id, _events in hub.events} == {OPERATOR_A, OPERATOR_B}
    assert all(events == [SandboxSessionsChangedEvent()] for _operator_id, events in hub.events)


async def _operator_ids() -> list[UUID]:
    return [OPERATOR_A, OPERATOR_B]


if __name__ == "__main__":
    pytest_bazel.main()
