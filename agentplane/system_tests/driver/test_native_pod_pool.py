"""Shared native Pod leases keep exact identities and discard failed or replaced guests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import create_autospec

import pytest

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver import native_pod_pool
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import Sandboxes

# gazelle:include_dep @pypi//protobuf


def _view(name: str, sandbox_uid: str, pod_uid: str) -> environment_service_pb2.EnvironmentDescriptor:
    view = environment_service_pb2.EnvironmentDescriptor(
        target=identity_pb2.EnvironmentTarget(namespace="test", name=name, uid=sandbox_uid)
    )
    view.observed_state.operating_mode = environment_service_pb2.OPERATING_MODE_RUNNING
    view.observed_state.readiness.state = environment_service_pb2.READINESS_STATE_READY
    view.kubernetes_diagnostics.pod.uid = pod_uid
    return view


@pytest.mark.parametrize(
    "failure",
    [
        "body",
        "target-replaced",
        "pod-replaced",
        "not-ready",
        "pod-deleting",
        "runner-not-ready",
        "pod-replaced-during-runner-readiness",
    ],
)
async def test_native_pod_reuses_healthy_guest_and_recreates_after_failed_lease(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    # Dataclass fields without defaults are instance attributes, absent from the class autospec.
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    created: list[environment_service_pb2.EnvironmentDescriptor] = []
    deleted: list[str] = []
    observed_failure: str | None = None
    runner_failure = False
    replace_pod_during_runner_readiness = False
    cleanup_should_fail = False

    @asynccontextmanager
    async def scope(_cluster: Cluster, _platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
        owned: list[str] = []

        async def create(name: str, *, template: str) -> environment_service_pb2.EnvironmentDescriptor:
            del template
            view = _view(name, f"sandbox-{len(created)}", f"pod-{len(created)}")
            created.append(view)
            owned.append(view.target.uid)
            return view

        try:
            yield create
        finally:
            deleted.extend(owned)
            if cleanup_should_fail:
                raise RuntimeError("cleanup failed")

    async def observed(target: identity_pb2.EnvironmentTarget) -> environment_service_pb2.EnvironmentDescriptor:
        view = next(view for view in created if view.target == target)
        result = environment_service_pb2.EnvironmentDescriptor()
        result.CopyFrom(view)
        match observed_failure:
            case "target-replaced":
                result.target.namespace = "replacement-namespace"
            case "pod-replaced":
                result.kubernetes_diagnostics.pod.uid = "replacement-pod"
            case "not-ready":
                result.observed_state.readiness.state = environment_service_pb2.READINESS_STATE_NOT_READY
            case "pod-deleting":
                result.kubernetes_diagnostics.pod.deleting = True
        return result

    async def ready_runner(_target: identity_pb2.EnvironmentTarget) -> None:
        nonlocal observed_failure
        if runner_failure:
            raise RuntimeError("runner is not ready")
        if replace_pod_during_runner_readiness:
            observed_failure = "pod-replaced"

    platform.environment.get_environment.side_effect = observed
    platform.ready_runner.side_effect = ready_runner
    monkeypatch.setattr(native_pod_pool, "native_sandbox_scope", scope)
    async with native_pod_pool.shared_pod(cluster, cast(NativePlatformClients, platform)) as borrow:
        async with borrow() as first:
            first_uid = first.target.uid
        async with borrow() as second:
            assert second.target.uid == first_uid
        assert len(created) == 1

        if failure == "body":
            cleanup_should_fail = True
            original = AssertionError("trust probe failed")
            with pytest.raises(AssertionError) as raised:
                async with borrow():
                    raise original
            assert raised.value is original
            cleanup_should_fail = False
        else:
            if failure == "runner-not-ready":
                runner_failure = True
            elif failure == "pod-replaced-during-runner-readiness":
                replace_pod_during_runner_readiness = True
            else:
                observed_failure = failure
            with pytest.raises(RuntimeError):
                async with borrow():
                    pytest.fail("an unhealthy lease must not be granted")
            runner_failure = False
            replace_pod_during_runner_readiness = False
            observed_failure = None
        assert deleted == [first_uid]

        async with borrow() as fresh:
            assert fresh.target.uid != first_uid
    assert deleted == [view.target.uid for view in created]


async def test_outer_failure_remains_primary_when_pool_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    created: list[environment_service_pb2.EnvironmentDescriptor] = []
    deleted: list[str] = []
    cleanup_should_fail = False

    @asynccontextmanager
    async def scope(_cluster: Cluster, _platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
        owned: list[str] = []

        async def create(name: str, *, template: str) -> environment_service_pb2.EnvironmentDescriptor:
            del template
            view = _view(name, "sandbox-owned", "pod-owned")
            created.append(view)
            owned.append(view.target.uid)
            return view

        try:
            yield create
        finally:
            deleted.extend(owned)
            if cleanup_should_fail:
                raise RuntimeError("cleanup failed")

    async def observed(target: identity_pb2.EnvironmentTarget) -> environment_service_pb2.EnvironmentDescriptor:
        del target
        view = environment_service_pb2.EnvironmentDescriptor()
        view.CopyFrom(created[0])
        return view

    platform.environment.get_environment.side_effect = observed
    monkeypatch.setattr(native_pod_pool, "native_sandbox_scope", scope)
    original = AssertionError("outer scenario failed")

    async def fail_outer() -> None:
        async with native_pod_pool.shared_pod(cluster, cast(NativePlatformClients, platform)) as borrow:
            async with borrow():
                pass
            raise original

    cleanup_should_fail = True
    with pytest.raises(AssertionError) as raised:
        await fail_outer()
    assert raised.value is original
    assert deleted == ["sandbox-owned"]
