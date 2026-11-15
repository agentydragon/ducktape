"""Opt-in fixtures for tests that need the production Go Runtime."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl, SVIDEndpoint
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.failure_journal import FailureEvidence, pytest_runtest_makereport
from agentplane.env.e2e.testing.fixtures import config, endpoint, harness, model, runner_gateway, spec, workspace
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.net.peertls.config import MTLS
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
    control = EnvironmentRuntimeControl(
        listen_address="127.0.0.1:9090",
        security=MTLS(
            tls=gateway_service.environment_service.issue_workload_tls(
                sandbox.environment_runtime_identity_uri, tmp_path / "environment-runtime-transport"
            )
        ),
    )
    launched = replace(config, launch=replace(config.launch, sandbox=sandbox.identity))
    # Scripted writers and component readers keep the ordinary History replica. The runner's
    # replica authenticates its actual Sandbox Identity certificate on the production SVID path.
    ingress_directory = tmp_path / "runner-history"
    ingress_directory.mkdir()
    async with (
        go_history_service(
            history_service.database_url,
            api=history_service.api,
            directory=ingress_directory,
            writer_namespaces=(sandbox.namespace,),
            svid_ca=gateway_service.environment_service.ca,
            policy_namespace=sandbox.namespace,
        ) as ingress_service,
        history_tunnel(ingress_service.ingress_target) as proxy,
        RunningRunner(
            launched,
            runner_gateway,
            history=history_service.reader,
            control_endpoint=control,
            ingress_endpoint=Endpoint(
                target=ingress_service.ingress_target,
                svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address=proxy),
            ),
            observe_control=False,
            controller_credentials=gateway_service.environment_service.channel_credentials(
                "spiffe://agentplane/sandbox-controller"
            ),
            incarnation_uid=sandbox.pod_uid,
        ) as started,
    ):
        await gateway_service.environment_service.wait_for_runner(sandbox_control, sandbox.destination)
        yield started
        if (evidence := FailureEvidence.for_failed_call(request)) is not None:
            await evidence.preserve_runner(started)


__all__ = [
    "config",
    "endpoint",
    "harness",
    "model",
    "pytest_runtest_makereport",
    "runner",
    "runner_gateway",
    "spec",
    "workspace",
]
