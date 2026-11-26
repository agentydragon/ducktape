"""The production Go Runtime as a fixture; it consumes `agentplane.env.e2e.testing.fixtures`."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.failure_journal import FailureEvidence
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.env.e2e.testing.runtime_with_history import run_runtime_with_history
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.testing.gateway_process import GatewayProcess


@pytest.fixture
async def runner(
    request: pytest.FixtureRequest,
    config: RunnerTestConfig,
    runner_gateway: Gateway,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    sandbox_control: SandboxControlClient,
    tmp_path: Path,
) -> AsyncIterator[RunningRunner]:
    """The production Go Runtime as `sandbox`'s, controlled through the Environments proxy; it still
    pushes its journal directly to the Go History Service ingress. Failed tests retain native
    evidence."""
    async with run_runtime_with_history(
        config,
        runner_gateway,
        history_service,
        sandbox,
        gateway_service.environment_service,
        sandbox_control,
        api=history_service.api,
        directory=tmp_path,
    ) as started:
        yield started
        if (evidence := FailureEvidence.for_failed_call(request)) is not None:
            await evidence.preserve_runner(started)
