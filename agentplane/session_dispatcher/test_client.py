"""The production Runtime client routes through the authenticated Environments proxy."""

import pytest

from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.grpc.calls import ServiceError
from agentplane.proto.identity_pb2 import SandboxDestination
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient

# gazelle:include_dep @pypi//protobuf


async def test_runner_client_routes_with_complete_sandbox_identity(
    sandbox: SandboxCluster, sandbox_control: SandboxControlClient, runner: RunningRunner
) -> None:
    assert await sandbox_control.list_sessions(sandbox.destination) == []

    assert await sandbox_control.list_sessions(sandbox.destination, expected_incarnation_uid=sandbox.pod_uid) == []
    with pytest.raises(ServiceError) as wrong_workload:
        await sandbox_control.list_sessions(sandbox.destination, expected_incarnation_uid="previous-workload")
    assert wrong_workload.value.code.name == "ABORTED"

    stale = SandboxDestination()
    stale.CopyFrom(sandbox.destination)
    stale.target.uid = "stale-sandbox-incarnation"
    with pytest.raises(ServiceError) as rejected:
        await sandbox_control.list_sessions(stale)
    assert rejected.value.code.name == "NOT_FOUND"
