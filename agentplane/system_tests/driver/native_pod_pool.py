"""Reuse an unchanged native Pod for trust probes, discarding it on any failed lease."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from uuid import uuid4

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cleanup_diagnostics import cleanup_without_masking
from agentplane.system_tests.driver.cleanup_task import finish_cleanup
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.native_sandbox import native_sandbox_scope
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE

# gazelle:include_dep @pypi//protobuf

type Borrow = Callable[[], AbstractAsyncContextManager[environment_service_pb2.EnvironmentDescriptor]]


@asynccontextmanager
async def shared_pod(cluster: Cluster, platform: NativePlatformClients) -> AsyncIterator[Borrow]:
    view: environment_service_pb2.EnvironmentDescriptor | None = None
    async with AsyncExitStack() as resources:

        @asynccontextmanager
        async def borrow() -> AsyncIterator[environment_service_pb2.EnvironmentDescriptor]:
            nonlocal view
            try:
                if view is None:
                    create = await resources.enter_async_context(native_sandbox_scope(cluster, platform))
                    view = await create(f"systest-trust-native-{uuid4().hex[:8]}", template=POD_TEMPLATE)
                observed = await platform.environment.get_environment(view.target)
                assert observed.target == view.target
                assert observed.kubernetes_diagnostics.HasField("pod")
                assert view.kubernetes_diagnostics.HasField("pod")
                assert observed.kubernetes_diagnostics.pod.uid == view.kubernetes_diagnostics.pod.uid
                await platform.ready_runner(view.target)
                yield view
            except BaseException as error:
                view = None
                await cleanup_without_masking(error, resources.pop_all().aclose)
                raise

        try:
            yield borrow
        finally:
            await finish_cleanup(resources.pop_all().aclose())
