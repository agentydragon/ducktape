"""The WatchEnvironments fold yields complete inventories and ignores stale removals."""

import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty

from agentplane.environments.testing.environment_inventory import EnvironmentInventory
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2


def _descriptor(name: str, uid: str) -> environment_service_pb2.EnvironmentDescriptor:
    return environment_service_pb2.EnvironmentDescriptor(
        target=identity_pb2.EnvironmentTarget(namespace="test", name=name, uid=uid)
    )


def _removed(name: str, uid: str) -> environment_service_pb2.WatchEnvironmentsResponse:
    return environment_service_pb2.WatchEnvironmentsResponse(
        removed=identity_pb2.EnvironmentTarget(namespace="test", name=name, uid=uid)
    )


def test_inventory_completes_snapshot_then_applies_changes() -> None:
    inventory = EnvironmentInventory()
    second, first = _descriptor("second", "second-uid"), _descriptor("first", "first-uid")
    replacement = _descriptor("first", "replacement-uid")

    chunk = environment_service_pb2.WatchEnvironmentsResponse(
        snapshot=environment_service_pb2.EnvironmentChunk(environments=[second, first])
    )
    assert inventory.apply(chunk) is None
    assert inventory.apply(environment_service_pb2.WatchEnvironmentsResponse(snapshot_complete=Empty())) == [
        first,
        second,
    ]
    assert inventory.apply(environment_service_pb2.WatchEnvironmentsResponse(upsert=replacement)) == [
        replacement,
        second,
    ]
    assert inventory.apply(_removed("first", "first-uid")) is None
    assert inventory.apply(_removed("first", "replacement-uid")) == [second]


@pytest.mark.parametrize(
    "frame",
    [
        environment_service_pb2.WatchEnvironmentsResponse(upsert=_descriptor("early", "early-uid")),
        _removed("early", "early-uid"),
    ],
)
def test_changes_before_snapshot_completion_are_rejected(
    frame: environment_service_pb2.WatchEnvironmentsResponse,
) -> None:
    with pytest.raises(RuntimeError, match="before snapshot completion"):
        EnvironmentInventory().apply(frame)


if __name__ == "__main__":
    pytest_bazel.main()
