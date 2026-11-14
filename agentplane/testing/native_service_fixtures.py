"""Production service composition without the app or an in-test Kubernetes cluster."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from python.runfiles import Runfiles

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl, SVIDEndpoint
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.fixtures import config, endpoint, model, spec, workspace
from agentplane.env.e2e.testing.gateway import bind_incarnation
from agentplane.env.e2e.testing.history_tunnel import history_tunnel
from agentplane.env.e2e.testing.model_config import model_config
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess, pid1_container
from agentplane.history_service.testing.fixtures import service_database_template, service_history_db_url
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.native_gateway_fixtures import (
    api_server,
    client_token,
    control,
    database_clones,
    database_names,
    dispatcher,
    dispatcher_database_url,
    gateway_service,
    gateway_token,
    kubeconfig,
    postgres_admin_url,
    postgres_container,
    postgres_image,
    runtime_client,
    sandbox,
)

__all__ = [
    "api_server",
    "client_token",
    "config",
    "control",
    "database_clones",
    "database_names",
    "dispatcher",
    "dispatcher_database_url",
    "endpoint",
    "gateway_service",
    "gateway_token",
    "history_service",
    "ingress",
    "kubeconfig",
    "model",
    "postgres_admin_url",
    "postgres_container",
    "postgres_image",
    "runtime_binary",
    "runtime_client",
    "sandbox",
    "service_database_template",
    "service_history_db_url",
    "spec",
    "start_runtime",
    "workspace",
]


@pytest.fixture
def runtime_binary() -> Path:
    """The production Runtime executable unless a test module overrides this fixture."""
    return Runfiles.CreateOrRaise().root() / launches.RUNTIME_BINARY


@pytest.fixture
async def history_service(
    service_history_db_url: str,
    api_server: KubernetesApiServer,
    sandbox: SandboxCluster,
    gateway_service: GatewayProcess,
    tmp_path: Path,
) -> AsyncIterator[GoHistoryService]:
    async with go_history_service(
        service_history_db_url,
        api=api_server,
        directory=tmp_path,
        writer_namespaces=(sandbox.namespace,),
        svid_ca=gateway_service.environment_runtime_ca,
        policy_namespace=sandbox.namespace,
    ) as service:
        yield service


@pytest.fixture
async def ingress(history_service: GoHistoryService) -> AsyncIterator[Endpoint]:
    async with history_tunnel(history_service.ingress_target) as proxy:
        yield Endpoint(
            target=history_service.ingress_target,
            svid=SVIDEndpoint(server_uri="spiffe://agentplane/history-ingress", proxy_address=proxy),
        )


@pytest.fixture
async def start_runtime(
    config: RunnerTestConfig,
    control: EnvironmentRuntimeControl,
    ingress: Endpoint,
    gateway_service: GatewayProcess,
    client_token: Path,
    sandbox: SandboxCluster,
    runtime_binary: Path,
    tmp_path: Path,
) -> AsyncIterator[Callable[[], Awaitable[RunnerProcess]]]:
    environment = {**config.harness_environment, "ANTHROPIC_AUTH_TOKEN": launches.TOKEN, "OTEL_SDK_DISABLED": "true"}
    remote = gateway_service.gateway_client(client_token, request_timeout_s=2)
    controller_credentials = gateway_service.controller_credentials(tmp_path / "controller-transport")
    async with model_config() as model_endpoint, AsyncExitStack() as processes:
        command = launches.command(
            config, control=control, ingress=ingress, model_config_target=model_endpoint, runner_binary=runtime_binary
        )

        async def stop(process: RunnerProcess) -> None:
            try:
                await process.stop()
            finally:
                await process.close()

        async def start() -> RunnerProcess:
            container = await asyncio.to_thread(pid1_container, command, tmp_path, environment)
            await asyncio.to_thread(container.__enter__)
            process = RunnerProcess(container)
            processes.push_async_callback(stop, process)
            await bind_incarnation(control, sandbox.uid, sandbox.pod_uid, controller_credentials)
            await gateway_service.wait_for_runner(remote, sandbox.destination)
            return process

        yield start
