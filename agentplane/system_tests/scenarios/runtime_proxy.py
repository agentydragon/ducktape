"""Exercise the enabled Environments receiver with its deployed certificates and routing watch."""

import asyncio

import grpc
import pytest

from agentplane.env.runtime.control import environment_runtime_pb2_grpc
from agentplane.environments.v1 import environment_service_pb2
from agentplane.grpc.calls import dial
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
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.tls import service_svid_credentials

# gazelle:include_dep @pypi//protobuf


async def environments_forwards_runtime_reads_and_refuses_stale_incarnations(
    cluster: Cluster, sandbox: environment_service_pb2.EnvironmentDescriptor
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
        async_port_forward(
            PortForward(
                cluster.command("port-forward"),
                resource="service/sandbox-controller",
                local_address="127.0.0.1",
                local_port=None,
                remote_port=8082,
                readiness_timeout_s=30,
            )
        ) as address,
        dial(
            address,
            channel_credentials=await service_svid_credentials(cluster, "app-workload-tls"),
            server_name=f"sandbox-controller.{cluster.namespace}.svc.cluster.local",
        ) as channel,
    ):
        runtime: environment_runtime_pb2_grpc.EnvironmentRuntimeAsyncStub = (
            environment_runtime_pb2_grpc.EnvironmentRuntimeStub(channel)
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
