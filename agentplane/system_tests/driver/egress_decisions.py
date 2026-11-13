"""Direct, explicitly authorized Egress decision reads for system tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agentplane.egress.api.admin import egress_admin_pb2 as pb, egress_admin_pb2_grpc as rpc
from agentplane.egress.api.rules import egress_rules_pb2
from agentplane.grpc.calls import dial
from agentplane.proto import identity_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import Caller, bound_caller
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.tls import service_credentials

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//types_grpcio
# gazelle:include_dep @pypi//types_protobuf


@asynccontextmanager
async def watch_client(cluster: Cluster) -> AsyncIterator[tuple[rpc.EgressAdminAsyncStub, Caller]]:
    # The app is an explicitly configured EgressAdmin reader; bind its test credential to its Pod.
    caller, _, _ = await bound_caller(cluster, "app", audience="agentplane-egress-admin")
    forward = PortForward(
        cluster.command("port-forward"),
        resource="service/egress-admin",
        local_address="127.0.0.1",
        local_port=None,
        remote_port=8081,
    )
    credentials = await service_credentials(cluster)
    async with (
        async_port_forward(forward) as target,
        dial(
            target, channel_credentials=credentials, server_name=f"egress-admin.{cluster.namespace}.svc.cluster.local"
        ) as channel,
    ):
        stub: rpc.EgressAdminAsyncStub = rpc.EgressAdminStub(channel)
        yield stub, caller


async def snapshot(
    stub: rpc.EgressAdminAsyncStub, caller: Caller, subject: identity_pb2.ServiceAccount
) -> list[pb.Decision]:
    return await _snapshot(stub, caller, pb.DecisionFilter(subject=subject))


async def snapshot_identity(stub: rpc.EgressAdminAsyncStub, caller: Caller, identity_name: str) -> list[pb.Decision]:
    return await _snapshot(
        stub, caller, pb.DecisionFilter(identity=egress_rules_pb2.IdentitySubject(name=identity_name))
    )


async def _snapshot(
    stub: rpc.EgressAdminAsyncStub, caller: Caller, decision_filter: pb.DecisionFilter
) -> list[pb.Decision]:
    stream = stub.WatchDecisions(pb.WatchDecisionsRequest(filter=decision_filter), metadata=caller.metadata)
    try:
        frame = await stream.read()
        if not isinstance(frame, pb.WatchDecisionsResponse) or frame.WhichOneof("frame") != "snapshot":
            raise RuntimeError("Egress decision watch ended without an initial snapshot")
        return list(frame.snapshot.decisions)
    finally:
        stream.cancel()
