"""Manual probe for Codex executor survival after a tool OOM."""

import os

import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.offered_models import models_offered_for
from agentplane.system_tests.driver.scenario import ScenarioResources
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.driver.tracing import traced_scenario
from agentplane.system_tests.scenarios import vm_sandbox


@pytest.mark.vm_sandboxes
@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_codex_tool_oom(
    cluster: Cluster,
    client: Client,
    dispatcher: SessionDispatcherClient,
    scenario_resources: ScenarioResources,
    scripted: ScriptedUpstream,
) -> None:
    models = await models_offered_for(client, [session_pb2.HARNESS_CODEX])
    async with scenario_resources() as (owned_client, sandbox):
        await traced_scenario(
            "codex-tool-oom",
            vm_sandbox.codex_tool_oom(
                cluster,
                owned_client,
                dispatcher,
                sandbox,
                models[session_pb2.HARNESS_CODEX],
                scripted,
                native_profile=os.environ.get("AGENTPLANE_VM_NATIVE_PROFILE") == "1",
            ),
        )


if __name__ == "__main__":
    pytest_bazel.main()
