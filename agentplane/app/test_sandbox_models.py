from uuid import uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.app.sandbox_models import sandbox_view
from agentplane.sandbox_service.protocol_pb2 import Sandbox, ServiceAccount, TeardownPhase

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize(
    ("teardown", "deleting"),
    [
        (TeardownPhase.TEARDOWN_PHASE_UNSPECIFIED, False),
        (TeardownPhase.TEARDOWN_PHASE_SEALING, True),
        (TeardownPhase.TEARDOWN_PHASE_SEALED, True),
    ],
)
def test_a_sandbox_being_torn_down_reads_as_deleting(teardown: TeardownPhase.ValueType, deleting: bool) -> None:
    view = Sandbox(
        name="test-sandbox",
        created_at=Timestamp(seconds=1),
        uid=str(uuid4()),
        operating_mode="Running",
        service_account=ServiceAccount(namespace="test-namespace", name="test-sandbox"),
        teardown=teardown,
    )
    assert sandbox_view(view).deleting is deleting


if __name__ == "__main__":
    pytest_bazel.main()
