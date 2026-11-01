"""MCP discovery, policy binding and Pod central sampler on one k3s stack."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.system_tests.cluster import Cluster
from agentplane.system_tests.executor import Executor
from agentplane.system_tests.harness_pair import run_harness_pair
from agentplane.system_tests.k3s.stack import Stack
from agentplane.system_tests.sandbox import Sandboxes
from agentplane.system_tests.scenarios import central_sampler, dcr, mcp
from agentplane.system_tests.scripted_upstream import ScriptedUpstream


async def test_deployed_discovery_and_registration(pytestconfig: pytest.Config, deployment: Stack) -> None:
    if pytestconfig.option.showlocals:
        pytest.fail("Disable pytest local-variable dumps before DCR", pytrace=False)
    assert deployment.mcp_fixtures is not None
    await dcr.deployed_discovery_and_registration(deployment.mcp_fixtures.mcp_edge_url)


@pytest.mark.scripted_model
async def test_policy_binding_auto_approves_the_bound_sandbox(
    operator_bff: httpx.AsyncClient,
    client: Client,
    sandbox: Sandboxes,
    scripted: ScriptedUpstream,
    policy_objects: mcp.PolicyObjects,
) -> None:
    await run_harness_pair(
        client,
        lambda harness, offered_model: mcp.policy_binding_auto_approves_the_bound_sandbox(
            operator_bff, client, sandbox, harness, offered_model, scripted, policy_objects
        ),
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_dispatcher_central_setup_tool_and_retained_answer(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.dispatcher_central_setup_tool_and_retained_answer(
        cluster, tmp_path, scripted, Executor(template="runner", cwd="/state/workspaces/central-acceptance")
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_pod_central_sampler_exec_resumes_after_sandbox_suspend_resume(
    cluster: Cluster, client: Client, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.pod_exec_session_rebinds_after_sandbox_suspend_resume(
        cluster, client, tmp_path, scripted, Executor(template="runner", cwd="/state/workspaces/central-resume")
    )


if __name__ == "__main__":
    pytest_bazel.main()
