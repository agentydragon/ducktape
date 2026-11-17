"""App-free central sampler service scenarios on the native fixture identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.executor import Executor
from agentplane.system_tests.driver.native_platform import NativePlatformClients
from agentplane.system_tests.driver.sandbox import POD_TEMPLATE
from agentplane.system_tests.driver.scripted_upstream import ScriptedUpstream
from agentplane.system_tests.scenarios import central_sampler

# gazelle:include_dep //agentplane/system_tests/deployment:pytest_plugin
# gazelle:include_dep //agentplane/system_tests/driver:native_fixtures
pytest_plugins = ("agentplane.system_tests.deployment.pytest_plugin", "agentplane.system_tests.driver.native_fixtures")


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_dispatcher_central_setup_tool_and_retained_answer(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.dispatcher_central_setup_tool_and_retained_answer(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-acceptance")
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_pod_central_sampler_exec_resumes_after_sandbox_suspend_resume(
    cluster: Cluster, native_platform: NativePlatformClients, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.pod_exec_session_rebinds_after_sandbox_suspend_resume(
        cluster,
        native_platform.environment,
        tmp_path,
        scripted,
        Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-resume"),
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_gateway_roll_keeps_live_central_read_and_turn(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.gateway_roll_keeps_live_central_read_and_turn(
        cluster, tmp_path, scripted, Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-gateway-roll")
    )


@pytest.mark.scripted_model
@pytest.mark.timeout(600)
async def test_gateway_exec_server_loss_notifies_active_central_turn(
    cluster: Cluster, tmp_path: Path, scripted: ScriptedUpstream
) -> None:
    await central_sampler.gateway_outage_restarts_exec_server_then_notifies_model(
        cluster,
        tmp_path,
        scripted,
        Executor(template=POD_TEMPLATE, cwd="/state/workspaces/central-gateway-native-loss"),
    )
