from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.sandbox_models import SandboxPhase as ViewPhase
from agentplane.app.sandbox_models import sandbox_view
from agentplane.proto.sandbox_service_pb2 import Sandbox, SandboxPhase, ServiceAccount

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize(
    ("phase", "view_phase"),
    [
        (SandboxPhase.SANDBOX_PHASE_CREATING, ViewPhase.CREATING),
        (SandboxPhase.SANDBOX_PHASE_RUNNING, ViewPhase.RUNNING),
        (SandboxPhase.SANDBOX_PHASE_SUSPENDING, ViewPhase.SUSPENDING),
        (SandboxPhase.SANDBOX_PHASE_SUSPENDED, ViewPhase.SUSPENDED),
        (SandboxPhase.SANDBOX_PHASE_SEALING, ViewPhase.SEALING),
        (SandboxPhase.SANDBOX_PHASE_TEARING_DOWN, ViewPhase.TEARING_DOWN),
    ],
)
def test_every_service_phase_has_a_view_phase(phase: SandboxPhase, view_phase: ViewPhase) -> None:
    view = Sandbox(
        name="test-sandbox",
        created_at=Timestamp(seconds=1),
        uid=str(uuid4()),
        operating_mode="Running",
        service_account=ServiceAccount(namespace="test-namespace", name="test-sandbox"),
        phase=phase,
    )
    assert sandbox_view(view).phase is view_phase


if __name__ == "__main__":
    pytest_bazel.main()
