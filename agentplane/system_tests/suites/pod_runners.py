"""A module-owned pool of protocol runners for the Pod system-test cases."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from uuid import uuid4

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import session_pb2
from agentplane.system_tests.driver.cleanup import finish_cleanup
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.scenario import open_resources
from agentplane.system_tests.driver.tracing import tracer

# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class Pair:
    claude: environment_service_pb2.EnvironmentDescriptor
    codex: environment_service_pb2.EnvironmentDescriptor

    def for_harness(self, harness: session_pb2.Harness) -> environment_service_pb2.EnvironmentDescriptor:
        match harness:
            case session_pb2.HARNESS_CLAUDE:
                return self.claude
            case session_pb2.HARNESS_CODEX:
                return self.codex
        raise ValueError(f"unsupported {harness=}")


class PoolUnavailableError(RuntimeError):
    """The runner pool cannot admit another borrowed pair."""


@dataclass(frozen=True)
class _Slot:
    pair: Pair
    client: Client
    resources: AsyncExitStack

    async def verify_identity(self) -> None:
        for previous in (self.pair.claude, self.pair.codex):
            current = await self.client.get_environment(previous.target)
            assert current.target.uid == previous.target.uid
            assert current.kubernetes_diagnostics.HasField("pod")
            assert previous.kubernetes_diagnostics.HasField("pod")
            assert current.kubernetes_diagnostics.pod.uid == previous.kubernetes_diagnostics.pod.uid, (
                f"shared runner was replaced: {previous.target.name}"
            )


class Pool:
    def __init__(self, cluster: Cluster, base_url: str, token: str) -> None:
        self._cluster = cluster
        self._base_url = base_url
        self._token = token
        self._idle: deque[_Slot] = deque()
        self._resources = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._poisoned: BaseException | None = None
        self._closed = False

    async def __aenter__(self) -> Pool:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> bool:
        del exc_type, traceback
        result = await finish_cleanup(self._close())
        if exc is not None:
            if result.failure is not None:
                exc.add_note(f"Pod runner pool cleanup also failed ({type(result.failure).__name__}).")
            return False
        if result.cancellation is not None:
            if result.failure is not None and result.failure is not result.cancellation:
                raise result.cancellation from result.failure
            raise result.cancellation
        if result.failure is not None:
            raise result.failure
        return False

    async def _close(self) -> None:
        async with self._lock:
            self._closed = True
            self._idle.clear()
            await self._resources.aclose()

    def _require_available(self) -> None:
        if self._closed or self._poisoned is not None:
            raise PoolUnavailableError("Pod runner pool is unavailable") from None

    def _poison(self, failure: BaseException) -> None:
        if self._poisoned is None:
            self._poisoned = failure

    async def _new_slot(self) -> _Slot:
        creating = AsyncExitStack()
        try:
            client, create = await creating.enter_async_context(
                open_resources(self._cluster, self._base_url, self._token, tracer, native_diagnostics=False)
            )
            prefix = f"systest-protocol-{uuid4().hex[:8]}"
            async with asyncio.TaskGroup() as starting:
                claude = starting.create_task(create(f"{prefix}-claude"))
                codex = starting.create_task(create(f"{prefix}-codex"))
        except BaseException as error:
            self._resources.push_async_callback(creating.aclose)
            result = await finish_cleanup(creating.aclose())
            if result.failure is not None:
                self._poison(result.failure)
                error.add_note(f"Pod runner creation cleanup also failed ({type(result.failure).__name__}).")
            raise
        slot = _Slot(
            pair=Pair(claude=claude.result(), codex=codex.result()), client=client, resources=creating.pop_all()
        )
        self._resources.push_async_callback(slot.resources.aclose)
        return slot

    async def _close_slots(self, slots: tuple[_Slot, ...]) -> None:
        results = await asyncio.gather(*(slot.resources.aclose() for slot in slots), return_exceptions=True)
        failures = [result for result in results if isinstance(result, BaseException)]
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Pod runner cleanup failed", failures)

    async def release_idle(self) -> None:
        async with self._lock:
            self._require_available()
            drained = tuple(self._idle)
            self._idle.clear()
            result = await finish_cleanup(self._close_slots(drained))
            if result.failure is not None:
                self._poison(result.failure)
                if result.cancellation is not None and result.failure is not result.cancellation:
                    raise result.cancellation from result.failure
                if result.cancellation is not None:
                    raise result.cancellation
                raise result.failure
            if result.cancellation is not None:
                raise result.cancellation

    async def _release(self, slot: _Slot, *, reuse: bool) -> None:
        async with self._lock:
            if reuse and not self._closed and self._poisoned is None:
                self._idle.append(slot)
                return
            try:
                await slot.resources.aclose()
            except BaseException as failure:
                self._poison(failure)
                raise

    @asynccontextmanager
    async def borrow(self) -> AsyncIterator[Pair]:
        async with self._lock:
            self._require_available()
            slot = self._idle.popleft() if self._idle else None

        if slot is None:
            slot = await self._new_slot()

        try:
            self._require_available()
            await slot.verify_identity()
            yield slot.pair
        except BaseException as error:
            result = await finish_cleanup(self._release(slot, reuse=False))
            if result.failure is not None:
                error.add_note(f"Pod runner cleanup also failed ({type(result.failure).__name__}).")
            raise
        else:
            result = await finish_cleanup(self._release(slot, reuse=True))
            if result.cancellation is not None:
                if result.failure is not None:
                    raise result.cancellation from result.failure
                raise result.cancellation
            if result.failure is not None:
                raise result.failure
