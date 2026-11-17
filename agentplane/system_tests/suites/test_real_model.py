"""Executable real_model scenarios sharing one k3s stack."""

from __future__ import annotations

import pytest

# gazelle:include_dep @pypi//protobuf
from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.session_dispatcher.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.sandbox import Sandboxes
from agentplane.system_tests.scenarios import egress, session_instructions

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep //agentplane/system_tests:app_fixtures
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin", "agentplane.system_tests.app_fixtures")


@pytest.mark.real_model
async def test_a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    harness: session_pb2.Harness,
    model: str,
) -> None:
    await egress.a_bound_sandbox_reaches_what_its_policy_names_and_nothing_else(
        cluster, client, dispatcher, sandbox, harness, model
    )


@pytest.mark.real_model
async def test_the_model_obeys_the_standing_instruction_its_session_was_created_with(
    client: Client, dispatcher: SessionDispatcherClient, sandbox: Sandboxes, harness: session_pb2.Harness, model: str
) -> None:
    await session_instructions.the_model_obeys_the_standing_instruction_its_session_was_created_with(
        client, dispatcher, sandbox, harness, model
    )
