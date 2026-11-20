"""Direct, explicitly authorized Egress decision reads for system tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agentplane.egress.api.admin import egress_admin_pb2 as pb, egress_admin_pb2_grpc as rpc
from agentplane.egress.api.rules import egress_rules_pb2
from agentplane.grpc.calls import dial
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.dispatcher_transport import forward
from agentplane.system_tests.driver.tls import service_svid_credentials

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//types_grpcio
# gazelle:include_dep @pypi//types_protobuf


@asynccontextmanager
async def watch_client(cluster: Cluster, *, svid_secret: str) -> AsyncIterator[rpc.EgressAdminAsyncStub]:
    """Read decisions over mTLS as the service identity in `svid_secret`."""
    credentials = await service_svid_credentials(cluster, svid_secret)
    async with (
        forward(cluster, "service/egress-admin", remote_port=8081) as target,
        dial(
            target, channel_credentials=credentials, server_name=f"egress-admin.{cluster.namespace}.svc.cluster.local"
        ) as channel,
    ):
        stub: rpc.EgressAdminAsyncStub = rpc.EgressAdminStub(channel)
        yield stub


async def snapshot_identity(stub: rpc.EgressAdminAsyncStub, identity_name: str) -> list[pb.Decision]:
    return await _snapshot(stub, pb.DecisionFilter(identity=egress_rules_pb2.IdentitySubject(name=identity_name)))


async def _snapshot(stub: rpc.EgressAdminAsyncStub, decision_filter: pb.DecisionFilter) -> list[pb.Decision]:
    stream = stub.WatchDecisions(pb.WatchDecisionsRequest(filter=decision_filter))
    try:
        frame = await stream.read()
        if not isinstance(frame, pb.WatchDecisionsResponse) or frame.WhichOneof("frame") != "snapshot":
            raise RuntimeError("Egress decision watch ended without an initial snapshot")
        return list(frame.snapshot.decisions)
    finally:
        stream.cancel()
