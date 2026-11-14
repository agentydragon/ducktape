"""Opt-in fixtures for tests that need the production Go Runtime."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import pytest

from agentplane.env.config import Endpoint, EnvironmentRuntimeControl, SVIDEndpoint
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.failure_journal import FailureEvidence, pytest_runtest_makereport
from agentplane.env.e2e.testing.fixtures import config, endpoint, harness, model, runner_gateway, spec, workspace
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.process import RunningRunner
from agentplane.history_service.testing.go_service import GoHistoryService, go_history_service
from agentplane.net.peertls.config import MTLS
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.testing.gateway_process import GatewayProcess
from agentplane.sandboxes.testing.kubernetes import SandboxCluster


@asynccontextmanager
async def _history_tunnel(target: str) -> AsyncIterator[str]:
    """Forward CONNECT bytes to this fixture's History listener; TLS stays end to end."""
    tasks: set[asyncio.Task[None]] = set()
    host, port = target.rsplit(":", 1)

    async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(10):
                headers = await reader.readuntil(b"\r\n\r\n")
            if headers.split(b"\r\n", 1)[0] != f"CONNECT {target} HTTP/1.1".encode():
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            remote, upstream = await asyncio.open_connection(host, int(port))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            forward = asyncio.create_task(pump(reader, upstream))
            backward = asyncio.create_task(pump(remote, writer))
            try:
                await asyncio.wait((forward, backward), return_when=asyncio.FIRST_COMPLETED)
            finally:
                forward.cancel()
                backward.cancel()
                await asyncio.gather(forward, backward, return_exceptions=True)
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
                with suppress(ConnectionError):
                    await upstream.wait_closed()
            with suppress(ConnectionError):
                await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    try:
        yield f"127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()
        pending = list(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


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
    """The production Go runner as `sandbox`'s, taking control through the Go Runner Gateway; it still
    pushes its journal directly to the Go History Service ingress. Failed tests retain native
    evidence."""
    control = EnvironmentRuntimeControl(
        listen_address="127.0.0.1:9090",
        security=MTLS(
            tls=gateway_service.issue_environment_runtime_tls(
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
            svid_ca=gateway_service.environment_runtime_ca,
            policy_namespace=sandbox.namespace,
        ) as ingress_service,
        _history_tunnel(ingress_service.ingress_target) as proxy,
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
            controller_credentials=gateway_service.controller_credentials(tmp_path / "controller-transport"),
            incarnation_uid=sandbox.pod_uid,
        ) as started,
    ):
        await gateway_service.wait_for_runner(sandbox_control, sandbox.destination)
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
