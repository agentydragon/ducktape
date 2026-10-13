from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.sandbox_models import sandbox_view
from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxPhase, ServiceAccount

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize(
    ("phase", "deleting"),
    [
        (SandboxPhase.SANDBOX_PHASE_RUNNING, False),
        (SandboxPhase.SANDBOX_PHASE_SUSPENDED, False),
        (SandboxPhase.SANDBOX_PHASE_SEALING, True),
        (SandboxPhase.SANDBOX_PHASE_TEARING_DOWN, True),
    ],
)
def test_a_sandbox_being_torn_down_reads_as_deleting(phase: SandboxPhase, deleting: bool) -> None:
    view = Sandbox(
        name="test-sandbox",
        created_at=Timestamp(seconds=1),
        uid=str(uuid4()),
        operating_mode="Running",
        service_account=ServiceAccount(namespace="test-namespace", name="test-sandbox"),
        phase=phase,
    )
    assert sandbox_view(view).deleting is deleting


if __name__ == "__main__":
    pytest_bazel.main()
