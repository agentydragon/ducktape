"""Executable real_model scenarios sharing one k3s stack."""

from __future__ import annotations

import pytest
import pytest_bazel

# gazelle:include_dep @pypi//protobuf
from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scenarios import egress, session_instructions


@pytest.mark.real_model
async def test_a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: runner_pb2.Harness,
    model: str,
) -> None:
    await egress.a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
        cluster, client, dispatcher, sandbox, harness, model
    )


@pytest.mark.real_model
async def test_the_model_obeys_the_standing_instruction_its_session_was_opened_with(
    client: Client, dispatcher: SessionDispatcherClient, sandbox: Sandboxes, harness: runner_pb2.Harness, model: str
) -> None:
    await session_instructions.the_model_obeys_the_standing_instruction_its_session_was_opened_with(
        client, dispatcher, sandbox, harness, model
    )


if __name__ == "__main__":
    pytest_bazel.main()
