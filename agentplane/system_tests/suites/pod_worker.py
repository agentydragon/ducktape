"""One sequential Pod case worker and its reusable Sandbox."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver import scenario
from agentplane.system_tests.driver.cleanup import close_resources, resource_scope
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import tracer

# Generated EnvironmentDescriptor interfaces import protobuf stubs.
# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class SandboxCase:
    client: Client
    sandbox: environment_service_pb2.EnvironmentDescriptor


@dataclass(frozen=True)
class FreshCase:
    client: Client
    sandboxes: Sandboxes


@dataclass
class Worker:
    cluster: Cluster
    base_url: str
    token: str
    scripted: ScriptedUpstream
    dispatcher: SessionDispatcherClient
    _sandbox: environment_service_pb2.EnvironmentDescriptor | None = field(default=None, init=False)
    _owned: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        cluster: Cluster,
        base_url: str,
        token: str,
        scripted: ScriptedUpstream,
        dispatcher: SessionDispatcherClient,
    ) -> AsyncIterator[Worker]:
        async with AsyncExitStack() as cleanup:
            worker = cls(cluster, base_url, token, scripted, dispatcher)
            cleanup.push_async_callback(worker.discard)
            yield worker

    async def discard(self, error: BaseException | None = None) -> None:
        self._sandbox = None
        await close_resources(self._owned.pop_all(), error)

    @asynccontextmanager
    async def reused_sandbox(self) -> AsyncIterator[SandboxCase]:
        try:
            async with (
                scenario.open_resources(self.cluster, self.base_url, self.token, tracer, native_diagnostics=False) as (
                    client,
                    _,
                ),
                self.scripted.case_scope(),
            ):
                if self._sandbox is None:
                    _, create = await self._owned.enter_async_context(
                        scenario.open_resources(
                            self.cluster, self.base_url, self.token, tracer, native_diagnostics=False
                        )
                    )
                    self._sandbox = await create(f"systest-protocol-{uuid4().hex[:8]}")
                current = await client.get_environment(self._sandbox.target)
                assert current.target.uid == self._sandbox.target.uid
                assert current.kubernetes_diagnostics.HasField("pod")
                assert self._sandbox.kubernetes_diagnostics.HasField("pod")
                assert current.kubernetes_diagnostics.pod.uid == self._sandbox.kubernetes_diagnostics.pod.uid, (
                    f"shared runner was replaced: {self._sandbox.target.name}"
                )
                yield SandboxCase(client, self._sandbox)
        except BaseException as error:
            await self.discard(error)
            raise

    @asynccontextmanager
    async def fresh_sandboxes(self) -> AsyncIterator[FreshCase]:
        await self.discard()
        async with resource_scope() as owned, self.scripted.case_scope():
            client, create = await owned.enter_async_context(
                scenario.open_resources(self.cluster, self.base_url, self.token, tracer, native_diagnostics=False)
            )
            yield FreshCase(client, create)
