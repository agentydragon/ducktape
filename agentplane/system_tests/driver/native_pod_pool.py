"""Reuse an unchanged native Pod for trust probes, discarding it on any failed lease."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from uuid import uuid4

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver.cleanup_diagnostics import cleanup_without_masking
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.native_sandbox import native_sandbox_scope
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE

# gazelle:include_dep @pypi//protobuf

type Borrow = Callable[[], AbstractAsyncContextManager[environment_service_pb2.EnvironmentDescriptor]]


def _complete_target(view: environment_service_pb2.EnvironmentDescriptor) -> identity_pb2.EnvironmentTarget:
    if not view.HasField("target") or not view.target.namespace or not view.target.name or not view.target.uid:
        raise RuntimeError("native Sandbox has no complete Environment target")
    target = identity_pb2.EnvironmentTarget()
    target.CopyFrom(view.target)
    return target


def _require_healthy_pod(
    view: environment_service_pb2.EnvironmentDescriptor,
    target: identity_pb2.EnvironmentTarget,
    expected_pod_uid: str | None = None,
) -> str:
    if not view.HasField("target") or view.target != target:
        raise RuntimeError("native Sandbox target changed")
    if (
        not view.HasField("observed_state")
        or view.observed_state.operating_mode != environment_service_pb2.OPERATING_MODE_RUNNING
        or not view.observed_state.HasField("readiness")
        or view.observed_state.readiness.state != environment_service_pb2.READINESS_STATE_READY
    ):
        raise RuntimeError("native Sandbox is not ready")
    if not view.HasField("kubernetes_diagnostics") or not view.kubernetes_diagnostics.HasField("pod"):
        raise RuntimeError("native Sandbox has no observed Pod")
    pod = view.kubernetes_diagnostics.pod
    if not pod.uid or pod.deleting:
        raise RuntimeError("native Sandbox Pod is not live")
    if expected_pod_uid is not None and pod.uid != expected_pod_uid:
        raise RuntimeError("native Sandbox Pod was replaced")
    return pod.uid


@asynccontextmanager
async def shared_pod(cluster: Cluster, platform: NativePlatformClients) -> AsyncIterator[Borrow]:
    owned: environment_service_pb2.EnvironmentDescriptor | None = None
    async with AsyncExitStack() as resources:

        @asynccontextmanager
        async def borrow() -> AsyncIterator[environment_service_pb2.EnvironmentDescriptor]:
            nonlocal owned
            try:
                if owned is None:
                    create = await resources.enter_async_context(native_sandbox_scope(cluster, platform))
                    owned = await create(f"systest-trust-native-{uuid4().hex[:8]}", template=POD_TEMPLATE)

                target = _complete_target(owned)
                pod_uid = _require_healthy_pod(owned, target)
                observed = await platform.environment.get_environment(target)
                _require_healthy_pod(observed, target, pod_uid)
                await platform.ready_runner(target)
                observed = await platform.environment.get_environment(target)
                _require_healthy_pod(observed, target, pod_uid)
                yield observed
            except BaseException as error:
                owned = None
                await cleanup_without_masking(error, resources.pop_all().aclose)
                raise

        try:
            yield borrow
        except BaseException as error:
            owned = None
            await cleanup_without_masking(error, resources.pop_all().aclose)
            raise
        else:
            owned = None
            await cleanup_without_masking(None, resources.pop_all().aclose)
