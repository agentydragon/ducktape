"""App-free Sandbox ownership through the native EnvironmentService."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from types import TracebackType

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver.cleanup_diagnostics import cleanup_without_masking
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.identity import (
    LAUNCH_DEFAULT_POLICY_SETS,
    create_scenario_identity,
    delete_scenario_identity,
)
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE, SANDBOX_READY_SECONDS, Sandboxes
from agentplane.system_tests.driver.sandbox_cleanup import delete_sandbox
from agentplane.system_tests.driver.sandbox_ownership import release_owned_sandboxes, remember_sandbox
from agentplane.system_tests.driver.tracing import tracer

# gazelle:include_dep @pypi//protobuf


async def wait_until_running(
    environment: EnvironmentServiceClient, target: identity_pb2.EnvironmentTarget
) -> environment_service_pb2.EnvironmentDescriptor:
    if not target.namespace or not target.name or not target.uid:
        raise ValueError("complete EnvironmentService target handle is required")
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
    """Own each acknowledged Sandbox UID before readiness work; retain Identity after uncertain create."""
    async with AsyncExitStack() as identities, AsyncExitStack() as sandboxes:
        owned: list[identity_pb2.EnvironmentTarget] = []
        sandboxes_released = False

        async def release(target: identity_pb2.EnvironmentTarget) -> None:
            if target.namespace != cluster.namespace:
                raise RuntimeError(
                    f"EnvironmentService target {target.namespace}/{target.name} is outside this fixture"
                )
            await delete_sandbox(cluster, target.name, target.uid)

        async def release_owned(
            _exc_type: type[BaseException] | None, exc: BaseException | None, _traceback: TracebackType | None
        ) -> bool:
            async def release_all() -> None:
                nonlocal sandboxes_released
                await release_owned_sandboxes(owned, release)
                sandboxes_released = True

            await cleanup_without_masking(exc, release_all)
            return False

        sandboxes.push_async_exit(release_owned)

        async def create(name: str, *, template: str = POD_TEMPLATE) -> environment_service_pb2.EnvironmentDescriptor:
            identity = await create_scenario_identity(cluster, name, list(LAUNCH_DEFAULT_POLICY_SETS))
            request = environment_service_pb2.CreateEnvironmentRequest(
                name=name, template=template, identity=identity["name"]
            )
            target = await platform.environment.create_environment(request)
            # Record an acknowledged create before checking any further contract or waiting for Ready.
            remember_sandbox(owned, target)
            if target.namespace != cluster.namespace:
                raise RuntimeError(f"EnvironmentService created {target.namespace}/{target.name} outside this fixture")

            async def delete_identity(
                _exc_type: type[BaseException] | None, exc: BaseException | None, _traceback: TracebackType | None
            ) -> bool:
                if not sandboxes_released:
                    if exc is not None:
                        exc.add_note("Scenario Identity retained because Sandbox deletion was not confirmed.")
                        return False
                    raise RuntimeError("Scenario Identity cannot be deleted before Sandbox deletion is confirmed")
                await cleanup_without_masking(exc, lambda: delete_scenario_identity(cluster, identity))
                return False

            # The identity is released only after every Sandbox that may reference it is gone.
            identities.push_async_exit(delete_identity)
            if target.name != name:
                raise RuntimeError(f"EnvironmentService created unexpected name {target.name!r} for {name!r}")
            with tracer.start_as_current_span(
                "system_test.sandbox.ready", attributes={"sandbox.name": name, "sandbox.uid": target.uid}
            ):
                view = await wait_until_running(platform.environment, target)
                await platform.ready_runner(target)
            return view

        yield create
