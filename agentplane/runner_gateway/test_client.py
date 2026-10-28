"""The production client routes through the authenticated Go Runner Gateway."""

import pytest
import pytest_bazel

from agentplane.grpc.calls import ServiceError
from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner.testing.process import RunningRunner
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.sandboxes.testing.kubernetes import SandboxCluster

# gazelle:include_dep @pypi//protobuf


async def test_runner_client_routes_with_complete_sandbox_identity(
    sandbox: SandboxCluster, sandbox_control: SandboxControlClient, runner: RunningRunner
) -> None:
    assert await sandbox_control.list_sessions(sandbox.destination) == []

    stale = SandboxDestination()
    stale.CopyFrom(sandbox.destination)
    stale.sandbox_uid = "stale-sandbox-incarnation"
    with pytest.raises(ServiceError) as rejected:
        await sandbox_control.list_sessions(stale)
    assert rejected.value.code.name == "NOT_FOUND"


if __name__ == "__main__":
    pytest_bazel.main()
