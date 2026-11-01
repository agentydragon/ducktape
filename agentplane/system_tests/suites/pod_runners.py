"""Leases for protocol-only cases that need fresh Sessions on unchanged Pod runners."""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from agentplane.app.testing.client import Client
from agentplane.app.testing.models import SandboxView
from agentplane.proto import runner_pb2
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.scenario import open_resources
from agentplane.system_tests.tracing import tracer

# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class Pair:
    claude: SandboxView
    codex: SandboxView

    def for_harness(self, harness: runner_pb2.Harness) -> SandboxView:
        match harness:
            case runner_pb2.HARNESS_CLAUDE:
                return self.claude
            case runner_pb2.HARNESS_CODEX:
                return self.codex
        raise ValueError(f"unsupported {harness=}")


type Borrow = Callable[[], AbstractAsyncContextManager[Pair]]


@dataclass(frozen=True)
class _Slot:
    pair: Pair
    client: Client
    resources: AsyncExitStack

    async def verify_identity(self) -> None:
        for previous in (self.pair.claude, self.pair.codex):
            current = await self.client.sandbox(previous.name)
            assert current.uid == previous.uid
            assert current.pod is not None
            assert previous.pod is not None
            assert current.pod.uid == previous.pod.uid, f"shared runner was replaced: {previous.name}"


@asynccontextmanager
async def pool(cluster: Cluster, base_url: str, token: str, *, limit: int) -> AsyncIterator[Borrow]:
    idle: deque[_Slot] = deque()
    available = asyncio.Semaphore(limit)
    async with AsyncExitStack() as pairs:

        @asynccontextmanager
        async def borrow() -> AsyncIterator[Pair]:
            async with available:
                if idle:
                    slot = idle.popleft()
                else:
                    async with AsyncExitStack() as creating:
                        client, create = await creating.enter_async_context(
                            open_resources(cluster, base_url, token, tracer, native_diagnostics=False)
                        )
                        prefix = f"systest-protocol-{uuid4().hex[:8]}"
                        async with asyncio.TaskGroup() as starting:
                            claude = starting.create_task(create(f"{prefix}-claude"))
                            codex = starting.create_task(create(f"{prefix}-codex"))
                        slot = _Slot(
                            pair=Pair(claude=claude.result(), codex=codex.result()),
                            client=client,
                            resources=creating.pop_all(),
                        )
                    pairs.push_async_callback(slot.resources.aclose)
                try:
                    await slot.verify_identity()
                    yield slot.pair
                except BaseException:
                    await slot.resources.aclose()
                    raise
                else:
                    idle.append(slot)

        yield borrow
