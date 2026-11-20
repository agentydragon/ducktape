"""Exercise the enabled Environments receiver with its deployed certificates and routing watch."""

import asyncio

import grpc
import pytest

from agentplane.app.testing.client import Client
from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.environments.v1 import environment_service_pb2, environment_service_pb2_grpc
from agentplane.grpc.calls import dial
from agentplane.harness_host import harness_host_pb2, harness_host_pb2_grpc
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.api import (
    INCARNATION_UID_HEADER,
    SANDBOX_HEADER,
    SANDBOX_NAMESPACE_HEADER,
    SANDBOX_OWNER_NAME_HEADER,
    SANDBOX_OWNER_NAMESPACE_HEADER,
    SANDBOX_UID_HEADER,
)
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import forward
from agentplane.system_tests.driver.native_auth import RUNTIME_PROXY_TEST_READER_SECRET
from agentplane.system_tests.driver.tls import service_svid_credentials

# gazelle:include_dep @pypi//protobuf


async def environments_forwards_runtime_reads_and_refuses_stale_incarnations(
    cluster: Cluster, client: Client, sandbox: environment_service_pb2.EnvironmentDescriptor
) -> None:
    target = sandbox.target
    assert sandbox.kubernetes_diagnostics.HasField("pod")
    incarnation = sandbox.kubernetes_diagnostics.pod.uid
    metadata = (
        (SANDBOX_HEADER, target.name),
        (SANDBOX_NAMESPACE_HEADER, target.namespace),
        (SANDBOX_UID_HEADER, target.uid),
        (SANDBOX_OWNER_NAMESPACE_HEADER, target.namespace),
        (SANDBOX_OWNER_NAME_HEADER, target.name),
    )
    async with (
        forward(cluster, "service/environments", remote_port=8082) as address,
        dial(
            address,
            channel_credentials=await service_svid_credentials(cluster, RUNTIME_PROXY_TEST_READER_SECRET),
            server_name=f"environments.{cluster.namespace}.svc.cluster.local",
        ) as runtime_channel,
        dial(
            address,
            channel_credentials=await service_svid_credentials(cluster, "app-workload-tls"),
            server_name=f"environments.{cluster.namespace}.svc.cluster.local",
        ) as channel,
    ):
        # A watch starts from the same exact environment after its transport reconnects.
        for _ in range(2):
            async with dial(
                address,
                channel_credentials=await service_svid_credentials(cluster, "app-workload-tls"),
                server_name=f"environments.{cluster.namespace}.svc.cluster.local",
            ) as watch_channel:
                watch = environment_service_pb2_grpc.EnvironmentServiceStub(watch_channel).WatchEnvironment(
                    environment_service_pb2.WatchEnvironmentRequest(target=target), timeout=15
                )
                observation = await watch.read()
                assert isinstance(observation, environment_service_pb2.WatchEnvironmentResponse)
                assert observation.environment.target == target
        runtime: environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub = (
            environment_runtime_pb2_grpc.EnvironmentRuntimeStub(runtime_channel)
        )
        # Ready proves the binding observer; the replica's forwarding watch may still be connecting.
        async with asyncio.timeout(30):
            while True:
                try:
                    status = await runtime.GetStatus(
                        session_pb2.GetStatusRequest(),
                        metadata=(*metadata, (INCARNATION_UID_HEADER, incarnation)),
                        timeout=5,
                    )
                    break
                except grpc.aio.AioRpcError as error:
                    if error.code() != grpc.StatusCode.UNAVAILABLE:
                        raise
                    await asyncio.sleep(0.1)
        assert status.environment_runtime_boot_id
        host = harness_host_pb2_grpc.HarnessHostStub(runtime_channel)
        host_list = host.ListSessions(harness_host_pb2.ListSessionsRequest(), metadata=metadata, timeout=5)
        assert ("x-agentplane-harness-host-session-version", "1") in tuple(await host_list.initial_metadata())
        host_sessions = await host_list
        for session in host_sessions.sessions:
            assert session.WhichOneof("host") == "environment_hosted"
            assert session.environment_hosted.harness.state != harness_host_pb2.HarnessAttachment.STATE_UNSPECIFIED
        assert len({session.session_id for session in host_sessions.sessions}) == len(host_sessions.sessions)
        with pytest.raises(grpc.aio.AioRpcError) as stale_host:
            await host.ListSessions(
                harness_host_pb2.ListSessionsRequest(),
                metadata=(*metadata, (INCARNATION_UID_HEADER, "stale-incarnation")),
                timeout=5,
            )
        assert stale_host.value.code() == grpc.StatusCode.ABORTED
        environments = environment_service_pb2_grpc.EnvironmentServiceStub(channel)
        diagnostics = await environments.GetRuntimeDiagnostics(
            environment_service_pb2.GetRuntimeDiagnosticsRequest(target=target), timeout=5
        )
        assert diagnostics.target == target
        assert diagnostics.incarnation_uid == incarnation
        assert diagnostics.HasField("observed_at")
        assert diagnostics.status.environment_runtime_boot_id == status.environment_runtime_boot_id
        app_status = await client.runtime_diagnostics(target)
        assert app_status.environment_runtime_boot_id == status.environment_runtime_boot_id
        app_runtime = environment_runtime_pb2_grpc.EnvironmentRuntimeStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as raw_status:
            await app_runtime.GetStatus(session_pb2.GetStatusRequest(), metadata=metadata, timeout=5)
        assert raw_status.value.code() == grpc.StatusCode.PERMISSION_DENIED
        app_host = harness_host_pb2_grpc.HarnessHostStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as raw_inventory:
            await app_host.ListSessions(harness_host_pb2.ListSessionsRequest(), metadata=metadata, timeout=5)
        assert raw_inventory.value.code() == grpc.StatusCode.PERMISSION_DENIED
        with pytest.raises(grpc.aio.AioRpcError) as stale:
            await runtime.GetStatus(
                session_pb2.GetStatusRequest(),
                metadata=(*metadata, (INCARNATION_UID_HEADER, "stale-incarnation")),
                timeout=5,
            )
        assert stale.value.code() == grpc.StatusCode.ABORTED
        with pytest.raises(grpc.aio.AioRpcError) as lifecycle:
            await runtime.Quiesce(session_pb2.QuiesceRequest(), metadata=metadata, timeout=5)
        assert lifecycle.value.code() == grpc.StatusCode.PERMISSION_DENIED
