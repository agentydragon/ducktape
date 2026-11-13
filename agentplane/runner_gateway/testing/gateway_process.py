"""Run the Runner Gateway against a supplied Kubernetes API and PostgreSQL."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import ExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import grpc
import yaml
from python.runfiles import Runfiles

from agentplane.environment_runtime_transport import config as runtime_transport
from agentplane.grpc.calls import ServiceError
from agentplane.identity.auth import listener as workload_listener
from agentplane.proto.identity_pb2 import SandboxDestination
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.runner_gateway.settings import Settings
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.go_process import go_process
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, make_ca, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.db_url import render_postgresql_url
from util.net import listen_tcp
from util.settings_contract import env_name

# gazelle:include_dep @pypi//protobuf


_GATEWAY_BINARY = "ducktape/agentplane/runner_gateway/runner-gateway_/runner-gateway"
_SESSION_DISPATCHER_URI = "spiffe://agentplane/session-dispatcher"


def _wire_database_url(database_url: str) -> str:
    return render_postgresql_url(database_url, "postgresql", query={"sslmode": "disable"})


def _migrate(binary: Path, database_url: str, log_path: Path) -> None:
    try:
        result = subprocess.run(
            [binary, "--migrate-only"],
            env={"AGENTPLANE_RUNNER_GATEWAY_DISPATCHER_DATABASE_URL": _wire_database_url(database_url)},
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
    log_path: Path
    environment_runtime_ca: CertificateAuthority
    environment_runtime_ca_file: Path
    dispatcher_audience: str
    gateway_audience: str

    @property
    def client_tls(self) -> runtime_transport.ClientTLS:
        return runtime_transport.ClientTLS(ca_file=self.environment_runtime_ca_file, server_uri=_SESSION_DISPATCHER_URI)

    def channel_credentials(self, *, client_certificate: tuple[Path, Path] | None = None) -> grpc.ChannelCredentials:
        """Verify the shared listener DNS SAN and optionally present a test workload SVID."""
        private_key = certificate_chain = None
        if client_certificate is not None:
            certificate_chain = client_certificate[0].read_bytes()
            private_key = client_certificate[1].read_bytes()
        return grpc.ssl_channel_credentials(
            root_certificates=self.environment_runtime_ca_file.read_bytes(),
            private_key=private_key,
            certificate_chain=certificate_chain,
        )

    def issue_environment_runtime_tls(self, uri: str, directory: Path) -> runtime_transport.TLS:
        """Issue an Environment Runtime peer certificate trusted by this Gateway process."""
        directory.mkdir(parents=True, exist_ok=True)
        cert_file, key_file = issue_workload_leaf(self.environment_runtime_ca, uri, directory)
        return runtime_transport.TLS(
            cert_file=str(cert_file), key_file=str(key_file), ca_file=str(self.environment_runtime_ca_file)
        )

    def gateway_client(self, token_file: Path, *, request_timeout_s: float = 20) -> SandboxControlClient:
        return SandboxControlClient(
            control_target=self.target,
            token_file=token_file,
            request_timeout_s=request_timeout_s,
            channel_credentials=self.channel_credentials(),
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
                if error.code not in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.ABORTED):
                    raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Runner proxy did not connect to Sandbox {destination.target.name}")
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


@asynccontextmanager
async def run_gateway(
    kubeconfig: Path,
    *,
    database_url: str,
    namespace: str,
    dispatcher_callers: Sequence[ServiceAccountRef],
    gateway_callers: Sequence[ServiceAccountRef],
    workload: ServiceAccountRef,
    executor_callers: Sequence[ServiceAccountRef] = (),
    environment_readers: Sequence[ServiceAccountRef] = (),
    environment_managers: Sequence[ServiceAccountRef] = (),
    operator_callers: Sequence[ServiceAccountRef] = (),
    dispatcher_audience: str,
    gateway_audience: str,
    runner_proxy_token_file: Path,
) -> AsyncIterator[GatewayProcess]:
    """Migrate the supplied database, then run the full Gateway on deployment-shaped settings.

    The Kubernetes API and PostgreSQL database are owned by the caller. The Goose migration
    runner applies the schema before the server starts. Platform and Sandbox RPCs share one TLS
    listener, with Dispatcher and Gateway bearer audiences scoped to their method families. Runner
    connections use each active Sandbox's direct mTLS gRPC server.
    """
    binary = Runfiles.CreateOrRaise().root() / _GATEWAY_BINARY
    migration_log = undeclared_outputs_dir() / f"go-runner-gateway-migration-{uuid4().hex}.log"
    await asyncio.to_thread(_migrate, binary, database_url, migration_log)

    log_path = undeclared_outputs_dir() / f"go-runner-gateway-{uuid4().hex}.log"
    # The database URL and Pod fields stay out of the file, as in deployment, where they come from
    # runtime sources. The explicit empty wake list exercises the authoring schema and Go loader contract.
    with ExitStack() as listeners:
        rpc_listener = listeners.enter_context(listen_tcp())
        with tempfile.TemporaryDirectory() as settings_dir:
            settings_directory = Path(settings_dir)
            environment_runtime_ca = make_ca("Environment Runtime transport test CA")
            environment_runtime_ca_file, _ = write_ca(
                environment_runtime_ca, settings_directory, "environment-runtime-ca"
            )
            gateway_tls_directory = settings_directory / "gateway-tls"
            gateway_tls_directory.mkdir()
            gateway_cert, gateway_key = issue_workload_leaf(
                environment_runtime_ca, "spiffe://agentplane/runner-gateway", gateway_tls_directory
            )
            rpc_tls_directory = settings_directory / "rpc-tls"
            rpc_tls_directory.mkdir()
            rpc_cert, rpc_key = issue_workload_leaf(
                environment_runtime_ca, _SESSION_DISPATCHER_URI, rpc_tls_directory, dns_name="localhost"
            )
            rpc_settings = workload_listener.Listener(
                listen_address=f"fd:{rpc_listener.socket.fileno()}",
                tls=workload_listener.TLS(cert_file=rpc_cert, key_file=rpc_key),
                auth=[
                    workload_listener.SPIFFEAuth(ca_file=environment_runtime_ca_file),
                    workload_listener.KubernetesBearerAuth(
                        audiences=frozenset({dispatcher_audience, gateway_audience})
                    ),
                ],
            )
            runner_security = runtime_transport.MTLS(
                tls=runtime_transport.TLS(
                    cert_file=str(gateway_cert), key_file=str(gateway_key), ca_file=str(environment_runtime_ca_file)
                )
            )
            effective_settings = Settings(
                sandbox_namespace=namespace,
                policy_namespace=namespace,
                dispatcher_database_url=database_url,
                pod_name=workload.name,
                pod_ip="192.0.2.10",
                runner_token_file=runner_proxy_token_file,
                kubeconfig=kubeconfig,
                rpc=rpc_settings,
                gateway_tls=runtime_transport.ClientTLS(
                    ca_file=environment_runtime_ca_file, server_uri=_SESSION_DISPATCHER_URI
                ),
                proxy_port=rpc_listener.port,
                runner_control_target_template=f"localhost:{rpc_listener.port}",
                dispatcher_audience=dispatcher_audience,
                gateway_audience=gateway_audience,
                runner_security=runner_security,
                dispatcher_callers={f"{account.namespace}/{account.name}" for account in dispatcher_callers},
                gateway_callers={f"{account.namespace}/{account.name}" for account in gateway_callers},
                executor_callers={f"{account.namespace}/{account.name}" for account in executor_callers},
                environment_readers={f"{account.namespace}/{account.name}" for account in environment_readers},
                environment_managers={f"{account.namespace}/{account.name}" for account in environment_managers},
                operator_callers={f"{account.namespace}/{account.name}" for account in operator_callers},
                wake_callers=set(),
                _cli_parse_args=[],
            )
            # These runtime-source settings stay out of the YAML, especially the database Secret.
            settings = effective_settings.model_dump(
                mode="json", exclude_none=True, exclude={"dispatcher_database_url", "pod_name", "pod_ip"}
            )
            for caller_list in (
                "dispatcher_callers",
                "gateway_callers",
                "executor_callers",
                "environment_readers",
                "environment_managers",
                "operator_callers",
                "wake_callers",
            ):
                settings[caller_list] = sorted(settings[caller_list])
            settings_path = settings_directory / "settings.yaml"
            settings_path.write_text(yaml.safe_dump(settings))
            environment = {
                **os.environ,
                Settings.config_file_env: str(settings_path),
                env_name(Settings, "dispatcher_database_url"): _wire_database_url(database_url),
                env_name(Settings, "pod_name"): workload.name,
                # Kubernetes rejects loopback EndpointSlice addresses. This advertised test address
                # is not dialed: this single-replica fixture sends RPCs to its loopback listener.
                env_name(Settings, "pod_ip"): "192.0.2.10",
            }
            credentials = grpc.ssl_channel_credentials(root_certificates=environment_runtime_ca_file.read_bytes())
            async with go_process(
                binary,
                env=environment,
                stderr=log_path,
                health_port=rpc_listener.port,
                grace_s=10,
                pass_fds=(rpc_listener.socket.fileno(),),
                health_credentials=credentials,
            ) as started:
                rpc_listener.socket.close()
                yield GatewayProcess(
                    process=started.process,
                    target=f"localhost:{rpc_listener.port}",
                    log_path=log_path,
                    environment_runtime_ca=environment_runtime_ca,
                    environment_runtime_ca_file=environment_runtime_ca_file,
                    dispatcher_audience=dispatcher_audience,
                    gateway_audience=gateway_audience,
                )
