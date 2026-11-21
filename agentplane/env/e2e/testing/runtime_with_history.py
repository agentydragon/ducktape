"""Start an exact Sandbox Runtime with authenticated History ingress and Environments control."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from agentplane.env.config import EnvironmentRuntimeControl
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.history_ingress import history_ingress
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.environments.testing.process import EnvironmentServiceProcess
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.net.peertls.config import MTLS, SANDBOX_CONTROLLER_URI
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer


@asynccontextmanager
async def run_runtime_with_history(
    config: RunnerTestConfig,
    runner_gateway: Gateway,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    environment_service: EnvironmentServiceProcess,
    sandbox_control: SandboxControlClient,
    *,
    api: KubernetesApiServer,
    directory: Path,
) -> AsyncIterator[RunningRunner]:
    control = EnvironmentRuntimeControl(
        listen_address="127.0.0.1:9090",
        security=MTLS(
            tls=environment_service.issue_workload_tls(
                sandbox.environment_runtime_identity_uri, directory / "environment-runtime-transport"
            )
        ),
    )
    launched = replace(config, launch=replace(config.launch, sandbox=sandbox.identity))
    # The second History replica verifies the Runtime's Sandbox Identity against the test API server.
    # Component readers and scripted writers keep using the original replica on the same database.
    ingress_directory = directory / "runner-history"
    ingress_directory.mkdir()
    async with (
        go_history_service(
            history_service.database_url,
            api=api,
            directory=ingress_directory,
            writer_namespaces=(sandbox.namespace,),
            svid_ca=environment_service.ca,
            policy_namespace=sandbox.namespace,
        ) as ingress_service,
        history_ingress(ingress_service.ingress_target) as ingress,
        RunningRunner(
            launched,
            runner_gateway,
            history=history_service.reader,
            control_endpoint=control,
            ingress_endpoint=ingress,
            observe_control=False,
            controller_credentials=environment_service.channel_credentials(SANDBOX_CONTROLLER_URI),
            incarnation_uid=sandbox.pod_uid,
        ) as started,
    ):
        await environment_service.wait_for_runner(sandbox_control, sandbox.destination)
        yield started
