"""Admission and cancellation behavior for the Pod runner pool."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import cast

import pytest
import pytest_bazel

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.suites import pod_runners


def _slot(
    *,
    close_started: asyncio.Event | None = None,
    close_gate: asyncio.Event | None = None,
    close_error: BaseException | None = None,
) -> pod_runners._Slot:
    closed = False

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        if close_started is not None:
            close_started.set()
        if close_gate is not None:
            await close_gate.wait()
        closed = True
        if close_error is not None:
            raise close_error

    resources = AsyncExitStack()
    resources.push_async_callback(close)
    return pod_runners._Slot(
        pair=pod_runners.Pair(
            claude=environment_service_pb2.EnvironmentDescriptor(),
            codex=environment_service_pb2.EnvironmentDescriptor(),
        ),
        client=cast(pod_runners.Client, object()),
        resources=resources,
    )


def _seed(pool: pod_runners.Pool, slot: pod_runners._Slot) -> None:
    pool._resources.push_async_callback(slot.resources.aclose)
    pool._idle.append(slot)


@pytest.mark.parametrize(
    "behavior", ["overlap", "concurrent_drain", "drain_cancellation", "poison", "return_lock_cancellation"]
)
async def test_pool_admission_finishes_drains_and_returns_before_releasing_cases(
    behavior: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with asyncio.timeout(3):
        pool = pod_runners.Pool(cast(Cluster, object()), "", "")
        created: list[pod_runners._Slot] = []
        new_slot_started = asyncio.Event()

        async def make_slot(self: pod_runners.Pool) -> pod_runners._Slot:
            slot = _slot()
            created.append(slot)
            self._resources.push_async_callback(slot.resources.aclose)
            new_slot_started.set()
            return slot

        monkeypatch.setattr(pod_runners.Pool, "_new_slot", make_slot)

        async def verify_identity(self: pod_runners._Slot) -> None:
            del self

        monkeypatch.setattr(pod_runners._Slot, "verify_identity", verify_identity)

        async with pool:
            if behavior == "overlap":
                active = asyncio.Event()
                fresh_started = asyncio.Event()
                finish = asyncio.Event()

                async def shared_case() -> None:
                    async with pool.borrow():
                        active.set()
                        await finish.wait()

                task = asyncio.create_task(shared_case())
                await active.wait()

                async def fresh_case() -> None:
                    await pool.release_idle()
                    fresh_started.set()

                await fresh_case()
                assert fresh_started.is_set()
                assert not task.done()
                finish.set()
                await task

            elif behavior == "concurrent_drain":
                close_gate = asyncio.Event()
                close_started_events = (asyncio.Event(), asyncio.Event())
                for started in close_started_events:
                    slot = _slot(close_started=started, close_gate=close_gate)
                    _seed(pool, slot)

                first_drain = asyncio.create_task(pool.release_idle())
                await asyncio.gather(*(started.wait() for started in close_started_events))
                second_drain = asyncio.create_task(pool.release_idle())
                fresh_waiting = asyncio.Event()

                async def fresh_case() -> None:
                    fresh_waiting.set()
                    async with pool.borrow():
                        pass

                fresh = asyncio.create_task(fresh_case())
                await fresh_waiting.wait()
                assert not new_slot_started.is_set()
                close_gate.set()
                await asyncio.gather(first_drain, second_drain, fresh)
                assert new_slot_started.is_set()
                assert len(created) == 1

            elif behavior == "drain_cancellation":
                close_started = asyncio.Event()
                close_gate = asyncio.Event()
                _seed(pool, _slot(close_started=close_started, close_gate=close_gate))
                drain = asyncio.create_task(pool.release_idle())
                await close_started.wait()
                drain.cancel()
                drain.cancel()
                close_gate.set()
                with pytest.raises(asyncio.CancelledError):
                    await drain
                async with pool.borrow():
                    pass
                assert new_slot_started.is_set()

            elif behavior == "poison":
                close_started = asyncio.Event()
                close_gate = asyncio.Event()
                _seed(
                    pool, _slot(close_started=close_started, close_gate=close_gate, close_error=RuntimeError("delete"))
                )
                drain = asyncio.create_task(pool.release_idle())
                await close_started.wait()
                admission_waiting = asyncio.Event()

                async def new_admission() -> None:
                    admission_waiting.set()
                    async with pool.borrow():
                        pass

                admission = asyncio.create_task(new_admission())
                await admission_waiting.wait()
                assert not new_slot_started.is_set()
                close_gate.set()
                with pytest.raises(RuntimeError, match="delete"):
                    await drain
                with pytest.raises(pod_runners.PoolUnavailableError):
                    await admission
                assert not created

            else:
                close_started = asyncio.Event()
                close_gate = asyncio.Event()
                returning_slot = _slot()
                draining_slot = _slot(close_started=close_started, close_gate=close_gate)
                _seed(pool, returning_slot)
                _seed(pool, draining_slot)
                body_started = asyncio.Event()
                finish_body = asyncio.Event()
                return_lock_waiting = asyncio.Event()
                release = pod_runners.Pool._release

                async def observe_return(self: pod_runners.Pool, slot: pod_runners._Slot, *, reuse: bool) -> None:
                    if slot is returning_slot and reuse:
                        return_lock_waiting.set()
                    await release(self, slot, reuse=reuse)

                monkeypatch.setattr(pod_runners.Pool, "_release", observe_return)

                async def returning_case() -> None:
                    async with pool.borrow():
                        body_started.set()
                        await finish_body.wait()

                returning = asyncio.create_task(returning_case())
                await body_started.wait()
                drain = asyncio.create_task(pool.release_idle())
                await close_started.wait()
                finish_body.set()
                await return_lock_waiting.wait()
                returning.cancel()
                returning.cancel()
                close_gate.set()
                await drain
                with pytest.raises(asyncio.CancelledError):
                    await returning
                assert returning_slot in pool._idle
                await pool.release_idle()


if __name__ == "__main__":
    pytest_bazel.main()
