from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.sandbox_models import AutosuspendBlocker, SandboxPhase as ViewPhase, sandbox_is_ready, sandbox_view
from agentplane.proto.sandboxes_pb2 import (
    AutosuspendBlocker as ProtoAutosuspendBlocker,
    Sandbox,
    SandboxPhase,
    ServiceAccount,
)
from agentplane.sandbox_resource import AutosuspendBlockerReason


@pytest.mark.parametrize(
    ("phase", "view_phase"),
    [
        (SandboxPhase.SANDBOX_PHASE_CREATING, ViewPhase.CREATING),
        (SandboxPhase.SANDBOX_PHASE_RUNNING, ViewPhase.RUNNING),
        (SandboxPhase.SANDBOX_PHASE_SUSPENDING, ViewPhase.SUSPENDING),
        (SandboxPhase.SANDBOX_PHASE_SUSPENDED, ViewPhase.SUSPENDED),
        (SandboxPhase.SANDBOX_PHASE_QUIESCING, ViewPhase.QUIESCING),
        (SandboxPhase.SANDBOX_PHASE_TEARING_DOWN, ViewPhase.TEARING_DOWN),
        (SandboxPhase.SANDBOX_PHASE_UNSPECIFIED, ViewPhase.UNSPECIFIED),
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


def test_absent_protobuf_mode_is_nullable_in_the_http_view() -> None:
    view = Sandbox(
        name="fresh",
        uid=str(uuid4()),
        created_at=Timestamp(seconds=1),
        service_account=ServiceAccount(namespace="test-namespace", name="fresh"),
        phase=SandboxPhase.SANDBOX_PHASE_UNSPECIFIED,
    )

    assert sandbox_view(view).operating_mode is None


def test_ready_condition_does_not_override_an_unreported_mode() -> None:
    view = Sandbox(
        name="fresh",
        uid=str(uuid4()),
        created_at=Timestamp(seconds=1),
        service_account=ServiceAccount(namespace="test-namespace", name="fresh"),
        status={"conditions": [{"type": "Ready", "status": "True"}]},
        phase=SandboxPhase.SANDBOX_PHASE_UNSPECIFIED,
    )

    assert not sandbox_is_ready(sandbox_view(view))


def test_sandbox_http_view_projects_blockers_with_nullable_snake_case_session_ids() -> None:
    view = Sandbox(
        name="busy",
        uid=str(uuid4()),
        created_at=Timestamp(seconds=1),
        service_account=ServiceAccount(namespace="test-namespace", name="busy"),
        phase=SandboxPhase.SANDBOX_PHASE_RUNNING,
        autosuspend_blockers=[
            ProtoAutosuspendBlocker(reason="PendingCommand", session_id="session-a"),
            ProtoAutosuspendBlocker(reason="Bootstrap"),
        ],
    )

    result = sandbox_view(view)

    assert result.autosuspend_blockers == [
        AutosuspendBlocker(reason=AutosuspendBlockerReason.PENDING_COMMAND, session_id="session-a"),
        AutosuspendBlocker(reason=AutosuspendBlockerReason.BOOTSTRAP, session_id=None),
    ]
    assert result.model_dump(mode="json")["autosuspend_blockers"] == [
        {"reason": "PendingCommand", "session_id": "session-a"},
        {"reason": "Bootstrap", "session_id": None},
    ]


if __name__ == "__main__":
    pytest_bazel.main()
