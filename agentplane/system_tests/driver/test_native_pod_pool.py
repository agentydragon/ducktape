"""Shared native Pod leases keep exact identities and discard failed or replaced guests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import create_autospec

import pytest
import pytest_bazel

from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver import native_pod_pool
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.environment_transport import EnvironmentServiceClient
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import Sandboxes

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize("failure", ["body", "sandbox-replaced", "pod-replaced", "readiness"])
async def test_native_pod_reuses_healthy_guest_and_recreates_after_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    cluster = Cluster(Path("/unused/kubeconfig"), "test", ("kubectl",))
    platform = create_autospec(NativePlatformClients, instance=True)
    # Dataclass fields without defaults are instance attributes, absent from the class autospec.
    platform.environment = create_autospec(EnvironmentServiceClient, instance=True)
    created: list[environment_service_pb2.EnvironmentDescriptor] = []
    deleted: list[str] = []

    @asynccontextmanager
    async def scope(_cluster: Cluster, _platform: NativePlatformClients) -> AsyncIterator[Sandboxes]:
        owned: list[str] = []

        async def create(name: str, *, template: str) -> environment_service_pb2.EnvironmentDescriptor:
            del template
            uid = f"sandbox-{len(created)}"
            view = environment_service_pb2.EnvironmentDescriptor(
                target=identity_pb2.EnvironmentTarget(namespace="test", name=name, uid=uid)
            )
            view.kubernetes_diagnostics.pod.uid = f"pod-{len(created)}"
            created.append(view)
            owned.append(uid)
            if failure == "readiness" and len(created) == 2:
                raise RuntimeError("readiness failed after acknowledgement")
            return view

        try:
            yield create
        finally:
            deleted.extend(owned)

    async def observed(target: identity_pb2.EnvironmentTarget) -> environment_service_pb2.EnvironmentDescriptor:
        return next(view for view in created if view.target == target)

    platform.environment.get_environment.side_effect = observed
    monkeypatch.setattr(native_pod_pool, "native_sandbox_scope", scope)
    async with native_pod_pool.shared_pod(cluster, cast(NativePlatformClients, platform)) as borrow:
        async with borrow() as first:
            first_uid = first.target.uid
        async with borrow() as second:
            assert second.target.uid == first_uid
        assert len(created) == 1
        with pytest.raises(AssertionError):
            async with borrow():
                raise AssertionError("failed trust probe")
        assert deleted == [first_uid]

        if failure == "readiness":
            with pytest.raises(RuntimeError, match="readiness failed"):
                async with borrow():
                    pytest.fail("readiness failure must not grant a lease")
            assert deleted == [view.target.uid for view in created]
        else:
            async with borrow():
                pass

            async def replaced(target: identity_pb2.EnvironmentTarget) -> environment_service_pb2.EnvironmentDescriptor:
                view = environment_service_pb2.EnvironmentDescriptor()
                view.CopyFrom(await observed(target))
                if failure == "sandbox-replaced":
                    view.target.uid = "replacement-sandbox"
                elif failure == "pod-replaced":
                    view.kubernetes_diagnostics.pod.uid = "replacement-pod"
                return view

            if failure != "body":
                platform.environment.get_environment.side_effect = replaced
                with pytest.raises(AssertionError):
                    async with borrow():
                        pytest.fail("replacement must not grant a lease")
                platform.environment.get_environment.side_effect = observed
        async with borrow() as fresh:
            assert fresh.target.uid != first_uid
    assert deleted == [view.target.uid for view in created]


if __name__ == "__main__":
    pytest_bazel.main()
