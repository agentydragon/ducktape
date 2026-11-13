"""Focused tests for the idle resume precondition."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, create_autospec

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.system_tests.scenarios import suspend_resume

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize("intermediate_stage", ["", "Quiescing", "Deleting"])
async def test_idle_suspend_waits_for_stopped_stage(intermediate_stage: str) -> None:
    client = create_autospec(Client, instance=True)
    target = identity_pb2.EnvironmentTarget(namespace="systest", name="box", uid="target-uid")

    def descriptor(stage: str) -> environment_service_pb2.EnvironmentDescriptor:
        return environment_service_pb2.EnvironmentDescriptor(
            target=target,
            observed_state=environment_service_pb2.EnvironmentObservedState(
                operating_mode=environment_service_pb2.OPERATING_MODE_SUSPENDED, suspended_by="Idle", stop_stage=stage
            ),
        )

    get_environment = cast(AsyncMock, client.get_environment)
    get_environment.side_effect = [descriptor(intermediate_stage), descriptor("Stopped")]

    stopped = await suspend_resume._wait_until_idle_suspended_without_pod(client, target)

    assert stopped.observed_state.stop_stage == "Stopped"
    assert get_environment.await_count == 2


if __name__ == "__main__":
    pytest_bazel.main()
