"""App-free Pod Sandbox ownership through the native EnvironmentService."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.identity import LAUNCH_DEFAULT_POLICY_SETS, scenario_identity
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, SANDBOX_READY_SECONDS, Sandboxes
from agentplane.system_tests.driver.sandbox_cleanup import delete_sandbox

# gazelle:include_dep @pypi//protobuf


async def wait_until_running(
    environment: EnvironmentServiceClient, target: identity_pb2.EnvironmentTarget
) -> environment_service_pb2.EnvironmentDescriptor:
    async with asyncio.timeout(SANDBOX_READY_SECONDS), aclosing(environment.environment_snapshots()) as snapshots:
        async for snapshot in snapshots:
            for view in snapshot:
                if view.target.namespace != target.namespace or view.target.name != target.name:
                    continue
                if view.target.uid != target.uid:
                    raise RuntimeError(f"Sandbox {target.name} was replaced before Ready")
                if (
                    view.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
                    and view.observed_state.HasField("readiness")
                    and view.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY
                ):
                    return view
    raise RuntimeError(f"Sandbox {target.name} inventory ended before Ready")


@asynccontextmanager
async def native_sandbox_scope(cluster: Cluster, platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
    async with AsyncExitStack() as identities, AsyncExitStack() as sandboxes:

        async def create(name: str, *, template: str = POD_TEMPLATE) -> environment_service_pb2.EnvironmentDescriptor:
            identity = await identities.enter_async_context(
                scenario_identity(cluster, name, list(LAUNCH_DEFAULT_POLICY_SETS))
            )
            target = await platform.environment.create_environment(
                environment_service_pb2.CreateEnvironmentRequest(name=name, template=template, identity=identity)
            )
            sandboxes.push_async_callback(delete_sandbox, cluster, target.name, target.uid)
            view = await wait_until_running(platform.environment, target)
            await platform.ready_runner(target)
            return view

        yield create
