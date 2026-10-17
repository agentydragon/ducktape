"""Run the unified Go Runner Gateway against a supplied Kubernetes API and PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from sqlalchemy.engine import make_url

from agentplane.background_task import background
from agentplane.grpc.calls import ServiceError, dial
from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner import control as runner_control
from agentplane.runner.reconnect import StreamTiming
from agentplane.runner.runner import Runner
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.subjects import ServiceAccountRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//types_protobuf


_GATEWAY_BINARY = "agentplane/runner_gateway/runner-gateway_/runner-gateway"
_MIGRATOR_BINARY = "agentplane/runner_gateway/migrations/cmd/runner-gateway-queue-migrate_/runner-gateway-queue-migrate"
_RUNNER_TIMING = StreamTiming(heartbeat_s=0.1, liveness_s=5, backoff_initial_s=0.05, backoff_max_s=0.5)


def _wire_database_url(database_url: str) -> str:
    return (
        make_url(database_url)
        .set(drivername="postgresql", query={"sslmode": "disable"})
        .render_as_string(hide_password=False)
    )


def _migrate(database_url: str, log_path: Path) -> None:
    migrator = get_required_path(own_repo_rlocation(_MIGRATOR_BINARY))
    try:
        result = subprocess.run(
            [str(migrator), "--database-url", _wire_database_url(database_url)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        log_path.write_text(f"migration timed out\nstdout:\n{error.stdout!r}\nstderr:\n{error.stderr!r}")
        raise RuntimeError(f"Go Runner Gateway migration timed out; see {log_path}") from error
    if result.returncode != 0:
        log_path.write_text(f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        raise RuntimeError(f"Go Runner Gateway migration failed with {result.returncode}; see {log_path}")


@dataclass
class GoService:
    """Addresses and process controls for one full unified Go Gateway process."""

    process: asyncio.subprocess.Process
    target: str
    log_path: Path

    def gateway_client(
        self, token_file: Path, *, request_timeout_s: float = 20, lifecycle_timeout_s: float = 310
    ) -> SandboxControlClient:
        return SandboxControlClient(
            control_target=self.target,
            token_file=token_file,
            request_timeout_s=request_timeout_s,
            lifecycle_timeout_s=lifecycle_timeout_s,
        )

    async def _check_process(self) -> None:
        if self.process.returncode is not None:
            raise RuntimeError(
                f"Go Runner Gateway exited with {self.process.returncode}; see undeclared output {self.log_path}"
            )

    async def wait_for_runner(
        self, client: SandboxControlClient, destination: SandboxDestination, *, timeout_s: float = 15
    ) -> None:
        """Probe the real Go proxy until the Runner Connect stream is serving calls."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            await self._check_process()
            try:
                await client.list_sessions(destination)
                return
            except ServiceError as error:
                if error.code != grpc.StatusCode.UNAVAILABLE:
                    raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Go Runner proxy did not connect to Sandbox {destination.sandbox}")
            await asyncio.sleep(min(0.05, remaining))

    @asynccontextmanager
    async def connected_runner(
        self, runner: Runner, *, token: str, proxy_client: SandboxControlClient, destination: SandboxDestination
    ) -> AsyncIterator[None]:
        """Connect a native Python Runner to this Go process and wait for proxy readiness."""
        client = runner_control.RunnerControlClient(
            runner_control.RunnerService(runner),
            target=self.target,
            bearer=token,
            timing=_RUNNER_TIMING,
            adopt_incarnation=runner.adopt_incarnation,
            facts=runner.facts,
            facts_changed=runner.facts_changed,
        )
        async with background(client.run(), name="native Runner to Go Gateway"):
            await self.wait_for_runner(proxy_client, destination)
            yield

    async def stop(self) -> None:
        if self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            async with asyncio.timeout(10):
                await self.process.wait()
        except TimeoutError:
            self.process.kill()
            await self.process.wait()


async def _while_running[T](service: GoService, operation: Awaitable[T], *, timeout_s: float) -> T:
    operation_task = asyncio.ensure_future(operation)
    exit_task = asyncio.create_task(service.process.wait())
    try:
        done, _ = await asyncio.wait(
            (operation_task, exit_task), timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if exit_task in done:
            raise RuntimeError(
                f"Go Runner Gateway exited with {service.process.returncode}; see undeclared output {service.log_path}"
            )
        if operation_task not in done:
            raise TimeoutError
        return await operation_task
    finally:
        for task in (operation_task, exit_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation_task, exit_task, return_exceptions=True)


async def _wait_health(service: GoService, target: str, *, timeout_s: float) -> None:
    async with dial(target) as channel:
        await _while_running(service, channel.channel_ready(), timeout_s=timeout_s)
        response = await _while_running(
            service,
            health_pb2_grpc.HealthStub(channel).Check(health_pb2.HealthCheckRequest(), timeout=min(1, timeout_s)),
            timeout_s=min(1, timeout_s),
        )
        if response.status != health_pb2.HealthCheckResponse.SERVING:
            raise ConnectionError(f"gRPC health at {target} is {response.status}")
    await service._check_process()


async def _wait_ready(service: GoService, *, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        await service._check_process()
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await _wait_health(service, service.target, timeout_s=remaining)
            return
        except ConnectionError, grpc.aio.AioRpcError, TimeoutError:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Go Runner Gateway did not become healthy; see undeclared output {service.log_path}"
                )
            await asyncio.sleep(min(0.05, remaining))


@asynccontextmanager
async def go_service(
    kubeconfig: Path,
    *,
    database_url: str,
    namespace: str,
    dispatcher_callers: Sequence[ServiceAccountRef],
    gateway_callers: Sequence[ServiceAccountRef],
    dispatcher_audience: str,
    gateway_audience: str,
    runner_audience: str,
    runner_proxy_token_file: Path,
    startup_timeout_s: float = 30,
) -> AsyncIterator[GoService]:
    """Migrate the supplied database, then run the full Go Gateway using production flags.

    The Kubernetes API and PostgreSQL clone are owned by the caller. The migration runner
    performs the same Alembic-to-Goose handoff used by the deployed process, before its server
    starts. All three gRPC services share a loopback listener, including the Dispatcher
    calling the Runner proxy.
    """
    migration_log = undeclared_outputs_dir() / f"go-runner-gateway-migration-{uuid4().hex}.log"
    await asyncio.to_thread(_migrate, database_url, migration_log)

    port = pick_free_port()
    binary = get_required_path(own_repo_rlocation(_GATEWAY_BINARY))
    log_path = undeclared_outputs_dir() / f"go-runner-gateway-{uuid4().hex}.log"
    arguments = [
        str(binary),
        f"--sandbox-namespace={namespace}",
        f"--runner-token-file={runner_proxy_token_file}",
        f"--kubeconfig={kubeconfig}",
        f"--listen-address=127.0.0.1:{port}",
        f"--proxy-port={port}",
        f"--runner-control-target-template=127.0.0.1:{port}",
        f"--dispatcher-audience={dispatcher_audience}",
        f"--gateway-audience={gateway_audience}",
        f"--runner-audience={runner_audience}",
        *(f"--dispatcher-caller={account.namespace}/{account.name}" for account in dispatcher_callers),
        *(f"--gateway-caller={account.namespace}/{account.name}" for account in gateway_callers),
    ]
    environment = {
        **os.environ,
        "AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": _wire_database_url(database_url),
        "AGENTPLANE_RUNNER_GATEWAY_POD_NAME": "runner-gateway-test",
        # Kubernetes rejects loopback EndpointSlice addresses. This advertised test address
        # is not dialed: this single-replica fixture sends RPCs to its loopback listener.
        "AGENTPLANE_RUNNER_GATEWAY_POD_IP": "192.0.2.10",
    }
    with log_path.open("wb") as log_file:
        process = await asyncio.create_subprocess_exec(
            *arguments, env=environment, stdout=log_file, stderr=subprocess.STDOUT
        )
        service = GoService(process=process, target=f"127.0.0.1:{port}", log_path=log_path)
        try:
            await _wait_ready(service, timeout_s=startup_timeout_s)
            yield service
        finally:
            await service.stop()
