"""Executable dispatcher scenarios sharing one k3s stack."""

from __future__ import annotations

from uuid import uuid4

import grpc
import pytest
import pytest_bazel

from agentplane.grpc.calls import dial
from agentplane.session_dispatcher.dispatcher.api.session import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.system_tests.deployment.stack import Stack
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import forward
from agentplane.system_tests.driver.native_auth import NATIVE_CLIENT_SVID_SECRET
from agentplane.system_tests.driver.tls import service_credentials, service_svid_credentials
from agentplane.system_tests.scenarios import dispatcher

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep @pypi//protobuf
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin",)


@pytest.fixture
def crictl_command(deployment: Stack) -> tuple[str, ...]:
    return (str(deployment.k3s.binary), "crictl")


async def test_sessions_service_authenticates_dispatcher_calls(cluster: Cluster) -> None:
    request = session_dispatcher_pb2.GetSessionRequest(session_id=str(uuid4()))
    async with (
        forward(cluster) as target,
        dial(
            target,
            channel_credentials=await service_svid_credentials(cluster, NATIVE_CLIENT_SVID_SECRET),
            server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
        ) as authorized,
        dial(
            target,
            channel_credentials=await service_credentials(cluster),
            server_name=f"sessions.{cluster.namespace}.svc.cluster.local",
        ) as anonymous,
    ):
        with pytest.raises(grpc.aio.AioRpcError) as absent_session:
            await session_dispatcher_pb2_grpc.SessionDispatcherStub(authorized).GetSession(request, timeout=15)
        assert absent_session.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.aio.AioRpcError) as unauthenticated:
            await session_dispatcher_pb2_grpc.SessionDispatcherStub(anonymous).GetSession(request, timeout=15)
        assert unauthenticated.value.code() == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.timeout(600)
@pytest.mark.parametrize("replica_failover", [False, True], ids=["queue", "replicas"])
async def test_deployed_dispatcher_waits_for_ready_then_records_runner_admission(
    crictl_command: tuple[str, ...], cluster: Cluster, replica_failover: bool
) -> None:
    await dispatcher.deployed_dispatcher_waits_for_ready_then_records_runner_admission(
        crictl_command, cluster, replica_failover
    )


@pytest.mark.timeout(600)
async def test_replica_dies_after_runtime_admission_and_peer_recovers_session(
    crictl_command: tuple[str, ...], cluster: Cluster
) -> None:
    await dispatcher.deployed_dispatcher_waits_for_ready_then_records_runner_admission(
        crictl_command, cluster, replica_failover=False, crash_mid_delivery=True
    )


@pytest.mark.timeout(600)
async def test_deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends(
    crictl_command: tuple[str, ...], cluster: Cluster
) -> None:
    await dispatcher.deployed_dispatcher_wakes_only_opted_in_sandbox_for_app_and_notification_sends(
        crictl_command, cluster
    )


if __name__ == "__main__":
    pytest_bazel.main()
