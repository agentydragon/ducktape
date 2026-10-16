"""The production client sends its per-Sandbox identity to the generated Runner API."""

from pathlib import Path

import pytest_bazel

from agentplane.proto.sandboxes_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner_gateway.testing.runner_peer import runner_peer

# gazelle:include_dep @pypi//protobuf


async def test_runner_client_sends_bearer_and_complete_sandbox_identity(tmp_path: Path) -> None:
    destination = SandboxDestination(
        owner=ServiceAccount(namespace="testing", name="native-runner"),
        sandbox="native-runner",
        sandbox_uid="native-runner-uid",
    )
    async with runner_peer(tmp_path / "caller-token", record_metadata=True, token="test-client-token") as endpoint:
        assert await endpoint.client().list_sessions(destination) == []

    assert len(endpoint.peer.metadata_calls) == 1
    method, metadata = endpoint.peer.metadata_calls[0]
    assert method == "ListSessions"
    assert metadata["authorization"] == ("Bearer test-client-token",)
    assert metadata["x-agentplane-sandbox"] == ("native-runner",)
    assert metadata["x-agentplane-sandbox-uid"] == ("native-runner-uid",)
    assert metadata["x-agentplane-sandbox-owner-namespace"] == ("testing",)
    assert metadata["x-agentplane-sandbox-owner-name"] == ("native-runner",)


if __name__ == "__main__":
    pytest_bazel.main()
