"""Run the Runner Gateway against a supplied Kubernetes API and PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
import yaml
from grpc_health.v1 import health_pb2, health_pb2_grpc
from sqlalchemy.engine import make_url

from agentplane.grpc.calls import ServiceError, dial
from agentplane.proto.sandboxes_pb2 import SandboxDestination
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.settings import Settings
from agentplane.subjects import ServiceAccountRef
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.net import listen_tcp
from util.settings_contract import env_name
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


_GATEWAY_BINARY = "agentplane/runner_gateway/runner-gateway_/runner-gateway"
_MIGRATOR_BINARY = "agentplane/runner_gateway/migrations/cmd/runner-gateway-queue-migrate_/runner-gateway-queue-migrate"


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
        raise RuntimeError(f"Runner Gateway migration timed out; see {log_path}") from error
    if result.returncode != 0:
        log_path.write_text(f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        raise RuntimeError(f"Runner Gateway migration failed with {result.returncode}; see {log_path}")


@dataclass
class GatewayProcess:
    """Addresses and process controls for one full Gateway process."""

    process: asyncio.subprocess.Process
    target: str
    # The Dispatcher's generated HTTP/JSON facade.
    http_url: str
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
                f"Runner Gateway exited with {self.process.returncode}; see undeclared output {self.log_path}"
            )

    async def wait_for_runner(
        self, client: SandboxControlClient, destination: SandboxDestination, *, timeout_s: float = 15
    ) -> None:
        """Probe the real Runner proxy until the Runner Connect stream is serving calls."""
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
                raise TimeoutError(f"Runner proxy did not connect to Sandbox {destination.sandbox}")
            await asyncio.sleep(min(0.05, remaining))

    async def stop(self) -> None:
        if self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
        try:
            async with asyncio.timeout(10):
                await self.process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()


async def _while_running[T](service: GatewayProcess, operation: Awaitable[T], *, timeout_s: float) -> T:
    operation_task = asyncio.ensure_future(operation)
    exit_task = asyncio.create_task(service.process.wait())
    try:
        done, _ = await asyncio.wait(
            (operation_task, exit_task), timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        if exit_task in done:
            raise RuntimeError(
                f"Runner Gateway exited with {service.process.returncode}; see undeclared output {service.log_path}"
            )
        if operation_task not in done:
            raise TimeoutError
        return await operation_task
    finally:
        for task in (operation_task, exit_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation_task, exit_task, return_exceptions=True)


async def _wait_health(service: GatewayProcess, target: str, *, timeout_s: float) -> None:
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


async def _wait_http_facade(service: GatewayProcess, *, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        while True:
            await service._check_process()
            try:
                response = await client.get(f"{service.http_url}/v1/dispatches")
            except httpx.TransportError:
                pass
            else:
                if response.status_code == 401:
                    return
                raise ConnectionError(f"HTTP facade returned {response.status_code} without credentials")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Runner Gateway HTTP facade did not become ready; see {service.log_path}")
            await asyncio.sleep(min(0.05, remaining))


async def _wait_ready(service: GatewayProcess, *, timeout_s: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        await service._check_process()
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await _wait_health(service, service.target, timeout_s=remaining)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            await _wait_http_facade(service, timeout_s=remaining)
            return
        except ConnectionError, grpc.aio.AioRpcError, TimeoutError:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Runner Gateway did not become healthy; see undeclared output {service.log_path}")
            await asyncio.sleep(min(0.05, remaining))


@asynccontextmanager
async def run_gateway(
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
) -> AsyncIterator[GatewayProcess]:
    """Migrate the supplied database, then run the full Gateway on deployment-shaped settings.

    The Kubernetes API and PostgreSQL database are owned by the caller. The Goose migration
    runner applies the schema before the server starts. All three gRPC services share a
    loopback listener, including the Dispatcher calling the Runner proxy.
    """
    migration_log = undeclared_outputs_dir() / f"go-runner-gateway-migration-{uuid4().hex}.log"
    await asyncio.to_thread(_migrate, database_url, migration_log)

    binary = get_required_path(own_repo_rlocation(_GATEWAY_BINARY))
    log_path = undeclared_outputs_dir() / f"go-runner-gateway-{uuid4().hex}.log"
    # The database URL and Pod fields stay out of the file, as in deployment, where they come from
    # runtime sources. The explicit empty wake list exercises the authoring schema and Go loader contract.
    with listen_tcp() as grpc_listener, listen_tcp() as http_listener:
        effective_settings = Settings(
            sandbox_namespace=namespace,
            dispatcher_database_url=database_url,
            pod_name="runner-gateway-test",
            pod_ip="192.0.2.10",
            runner_token_file=runner_proxy_token_file,
            kubeconfig=kubeconfig,
            listen_address=f"fd:{grpc_listener.socket.fileno()}",
            http_listen_address=f"fd:{http_listener.socket.fileno()}",
            proxy_port=grpc_listener.port,
            runner_control_target_template=f"{grpc_listener.host}:{grpc_listener.port}",
            dispatcher_audience=dispatcher_audience,
            gateway_audience=gateway_audience,
            runner_audience=runner_audience,
            dispatcher_callers={f"{account.namespace}/{account.name}" for account in dispatcher_callers},
            gateway_callers={f"{account.namespace}/{account.name}" for account in gateway_callers},
            wake_callers=set(),
            _cli_parse_args=[],
        )
        # These runtime-source settings stay out of the YAML, especially the database Secret.
        settings = effective_settings.model_dump(
            mode="json",
            exclude_none=True,
            exclude_unset=True,
            exclude={"dispatcher_database_url", "pod_name", "pod_ip"},
        )
        for caller_list in ("dispatcher_callers", "gateway_callers", "wake_callers"):
            settings[caller_list] = sorted(settings[caller_list])
        with tempfile.TemporaryDirectory() as settings_dir, log_path.open("wb") as log_file:
            settings_path = Path(settings_dir) / "settings.yaml"
            settings_path.write_text(yaml.safe_dump(settings))
            environment = {
                **os.environ,
                Settings.config_file_env: str(settings_path),
                env_name(Settings, "dispatcher_database_url"): _wire_database_url(database_url),
                env_name(Settings, "pod_name"): "runner-gateway-test",
                # Kubernetes rejects loopback EndpointSlice addresses. This advertised test address
                # is not dialed: this single-replica fixture sends RPCs to its loopback listener.
                env_name(Settings, "pod_ip"): "192.0.2.10",
            }
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    str(binary),
                    env=environment,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    pass_fds=[grpc_listener.socket.fileno(), http_listener.socket.fileno()],
                )
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await asyncio.shield(spawn)
                orphan = GatewayProcess(
                    process=process,
                    target=f"{grpc_listener.host}:{grpc_listener.port}",
                    http_url=f"http://{http_listener.host}:{http_listener.port}",
                    log_path=log_path,
                )
                cleanup = asyncio.create_task(orphan.stop())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)
                    raise
                raise
            grpc_listener.socket.close()
            http_listener.socket.close()
            service = GatewayProcess(
                process=process,
                target=f"{grpc_listener.host}:{grpc_listener.port}",
                http_url=f"http://{http_listener.host}:{http_listener.port}",
                log_path=log_path,
            )
            try:
                await _wait_ready(service, timeout_s=startup_timeout_s)
                yield service
            finally:
                cleanup = asyncio.create_task(service.stop())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup)
                    raise
