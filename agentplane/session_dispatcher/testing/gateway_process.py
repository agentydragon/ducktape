"""Run the Runner Gateway against a supplied Kubernetes API and PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import grpc
import yaml
from python.runfiles import Runfiles

from agentplane.environments.testing.process import EnvironmentServiceProcess, run_environment_service
from agentplane.identity.auth import listener as workload_listener
from agentplane.net.peertls import config as runtime_transport
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.session_dispatcher.settings import Settings
from agentplane.testing.go_process import GoProcess, RestartGoProcess, WhileStopped, go_process, restartable_go_process
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, make_ca, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.db_url import render_postgresql_url
from util.net import listen_tcp

# gazelle:include_dep @pypi//protobuf


_GATEWAY_BINARY = "ducktape/agentplane/session_dispatcher/cmd/runner-gateway_/runner-gateway"
_SESSION_DISPATCHER_URI = "spiffe://agentplane/session-dispatcher"


def _wire_database_url(database_url: str) -> str:
    return render_postgresql_url(database_url, "postgresql", query={"sslmode": "disable"})


def _migrate(binary: Path, database_url: str, log_path: Path) -> None:
    try:
        result = subprocess.run(
            [binary, "--migrate-only"],
            env={"AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL": _wire_database_url(database_url)},
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
    """Dispatcher process controls plus its separately addressed Environments peer."""

    process: asyncio.subprocess.Process
    target: str
    environment_service: EnvironmentServiceProcess
    service_certificates: dict[str, tuple[Path, Path]]
    _restart: RestartGoProcess
    _close_listener: Callable[[], None]

    @property
    def client_tls(self) -> runtime_transport.ClientTLS:
        return self.service_tls(runtime_transport.APP_URI)

    def service_tls(self, service: str) -> runtime_transport.ClientTLS:
        cert_file, key_file = self.service_certificates[service]
        return runtime_transport.ClientTLS(
            ca_file=self.environment_service.ca_file,
            server_uri=_SESSION_DISPATCHER_URI,
            cert_file=cert_file,
            key_file=key_file,
            client_uri=service,
        )

    def service_credentials(self, service: str) -> grpc.ChannelCredentials:
        return self.channel_credentials(client_certificate=self.service_certificates[service])

    def channel_credentials(self, *, client_certificate: tuple[Path, Path] | None = None) -> grpc.ChannelCredentials:
        """Verify the shared listener DNS SAN and optionally present a test workload SVID."""
        private_key = certificate_chain = None
        if client_certificate is not None:
            certificate_chain = client_certificate[0].read_bytes()
            private_key = client_certificate[1].read_bytes()
        return grpc.ssl_channel_credentials(
            root_certificates=self.environment_service.ca_file.read_bytes(),
            private_key=private_key,
            certificate_chain=certificate_chain,
        )

    def environment_runtime_client(
        self, service: str = runtime_transport.APP_URI, *, request_timeout_s: float = 20
    ) -> SandboxControlClient:
        """Call Runtime through this explicitly separate Environments listener."""
        return SandboxControlClient(
            control_target=self.environment_service.target,
            request_timeout_s=request_timeout_s,
            channel_credentials=self.environment_service.channel_credentials(service),
        )

    async def stop(self) -> None:
        self._close_listener()
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

    async def restart(self, *, while_stopped: WhileStopped | None = None) -> None:
        """Replace the process while retaining its listener, database and deployment settings."""
        started = await self._restart(while_stopped)
        self.process = started.process


@asynccontextmanager
async def run_gateway(
    kubeconfig: Path,
    *,
    database_url: str,
    namespace: str,
    runtime_proxy_controller_callers: Sequence[str] = (),
    runtime_proxy_executor_callers: Sequence[str] = (),
    environment_runtime_ca: CertificateAuthority | None = None,
) -> AsyncIterator[GatewayProcess]:
    """Run the Dispatcher and an independent production Environments Runtime proxy peer."""
    binary = Runfiles.CreateOrRaise().root() / _GATEWAY_BINARY
    migration_log = undeclared_outputs_dir() / f"go-runner-gateway-migration-{uuid4().hex}.log"
    await asyncio.to_thread(_migrate, binary, database_url, migration_log)

    log_path = undeclared_outputs_dir() / f"go-runner-gateway-{uuid4().hex}.log"
    # The database URL stays out of the file, as in deployment, where it comes from a runtime source.
    # Exact service roles exercise the authoring schema and Go loader contract.
    if environment_runtime_ca is None:
        environment_runtime_ca = make_ca("Environment Runtime transport test CA")
    async with run_environment_service(
        kubeconfig,
        namespace,
        caller_uri=runtime_transport.APP_URI,
        environment_runtime_ca=environment_runtime_ca,
        runtime_proxy_controller_callers=runtime_proxy_controller_callers,
        runtime_proxy_executor_callers=runtime_proxy_executor_callers,
    ) as environment_service:
        with ExitStack() as listeners:
            rpc_listener = listeners.enter_context(listen_tcp())
            with tempfile.TemporaryDirectory() as settings_dir:
                settings_directory = Path(settings_dir)
                environment_runtime_ca_file, _ = write_ca(
                    environment_runtime_ca, settings_directory, "environment-runtime-ca"
                )
                rpc_tls_directory = settings_directory / "rpc-tls"
                rpc_tls_directory.mkdir()
                rpc_cert, rpc_key = issue_workload_leaf(
                    environment_runtime_ca, _SESSION_DISPATCHER_URI, rpc_tls_directory, dns_name="localhost"
                )
                rpc_settings = workload_listener.Listener(
                    listen_address=f"fd:{rpc_listener.socket.fileno()}",
                    tls=workload_listener.TLS(cert_file=rpc_cert, key_file=rpc_key),
                    auth=[workload_listener.SPIFFEAuth(ca_file=environment_runtime_ca_file)],
                )
                service_certificates = {}
                for service in (runtime_transport.APP_URI, runtime_transport.CENTRAL_SAMPLER_URI):
                    directory = settings_directory / service.rsplit("/", 1)[-1]
                    directory.mkdir()
                    service_certificates[service] = issue_workload_leaf(environment_runtime_ca, service, directory)
                effective_settings = Settings(
                    sandbox_namespace=namespace,
                    policy_namespace=namespace,
                    kubeconfig=kubeconfig,
                    rpc=rpc_settings,
                    gateway_tls=environment_service.tls_for(runtime_transport.GATEWAY_SESSIONS_URI),
                    runner_control_target_template=environment_service.target,
                    dispatcher_service_callers={runtime_transport.APP_URI},
                )
                settings = effective_settings.model_dump(mode="json", exclude_none=True)
                settings_path = settings_directory / "settings.yaml"
                settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
                environment = {
                    **{
                        key: value
                        for key, value in os.environ.items()
                        if not key.startswith("AGENTPLANE_RUNNER_GATEWAY_")
                    },
                    "AGENTPLANE_SESSIONS_CONFIG_FILE": str(settings_path),
                    "AGENTPLANE_SESSIONS_DISPATCHER_DATABASE_URL": _wire_database_url(database_url),
                }
                credentials = grpc.ssl_channel_credentials(root_certificates=environment_runtime_ca_file.read_bytes())

                def process_context(generation: int) -> AbstractAsyncContextManager[GoProcess]:
                    generation_log = log_path.with_name(f"{log_path.stem}-{generation}{log_path.suffix}")
                    return go_process(
                        binary,
                        env=environment,
                        stderr=generation_log,
                        health_port=rpc_listener.port,
                        grace_s=10,
                        pass_fds=(rpc_listener.socket.fileno(),),
                        health_credentials=credentials,
                    )

                async with restartable_go_process(process_context) as (started, restart_process):
                    gateway = GatewayProcess(
                        process=started.process,
                        target=f"localhost:{rpc_listener.port}",
                        environment_service=environment_service,
                        service_certificates=service_certificates,
                        _restart=restart_process,
                        _close_listener=rpc_listener.socket.close,
                    )
                    yield gateway
