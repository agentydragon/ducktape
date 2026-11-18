"""Run the production EnvironmentService and Runtime proxy listeners in a child process."""

# gazelle:include_dep @pypi//protobuf

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import grpc
from python.runfiles import Runfiles

from agentplane.grpc.calls import ServiceError
from agentplane.net.peertls.config import (
    APP_URI,
    GATEWAY_RUNTIME_PROXY_URI,
    GATEWAY_SESSIONS_URI,
    SANDBOX_CONTROLLER_URI,
    TLS,
    ClientTLS,
)
from agentplane.proto.identity_pb2 import SandboxDestination
from agentplane.session_dispatcher.client import SandboxControlClient
from agentplane.testing.go_process import GoProcess, RestartGoProcess, WhileStopped, go_process, restartable_go_process
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, make_ca, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import listen_tcp


@dataclass
class EnvironmentServiceProcess:
    """One production EnvironmentService peer and its Runtime proxy receiver."""

    target: str
    client_uri: str
    ca: CertificateAuthority
    ca_file: Path
    service_certificates: dict[str, tuple[Path, Path]]
    process: asyncio.subprocess.Process
    log_path: Path
    _restart: RestartGoProcess

    @property
    def tls(self) -> ClientTLS:
        """TLS for the primary caller supplied to `run_environment_service`."""
        return self.tls_for(self.client_uri)

    def tls_for(self, service: str) -> ClientTLS:
        cert_file, key_file = self.service_certificates[service]
        return ClientTLS(
            ca_file=self.ca_file,
            server_uri=SANDBOX_CONTROLLER_URI,
            cert_file=cert_file,
            key_file=key_file,
            client_uri=service,
        )

    def channel_credentials(self, service: str) -> grpc.ChannelCredentials:
        cert_file, key_file = self.service_certificates[service]
        return grpc.ssl_channel_credentials(
            root_certificates=self.ca_file.read_bytes(),
            private_key=key_file.read_bytes(),
            certificate_chain=cert_file.read_bytes(),
        )

    def issue_workload_tls(self, uri: str, directory: Path) -> TLS:
        """Issue a Sandbox workload certificate trusted by this Environments peer."""
        directory.mkdir(parents=True, exist_ok=True)
        cert_file, key_file = issue_workload_leaf(self.ca, uri, directory)
        return TLS(cert_file=str(cert_file), key_file=str(key_file), ca_file=str(self.ca_file))

    async def restart(self, *, while_stopped: WhileStopped | None = None) -> None:
        """Replace the Environments process while retaining its listener, CA and credentials."""
        started = await self._restart(while_stopped)
        self.process = started.process
        self.log_path = started.stderr

    async def wait_for_runner(
        self, client: SandboxControlClient, destination: SandboxDestination, *, timeout_s: float = 15
    ) -> None:
        """Wait until this peer can observe the Sandbox Runtime through its production proxy."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if self.process.returncode is not None:
                raise RuntimeError(f"EnvironmentService exited with {self.process.returncode}; see {self.log_path}")
            try:
                await client.list_sessions(destination)
                return
            except ServiceError as error:
                if error.code not in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.ABORTED):
                    raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Runtime proxy did not connect to Sandbox {destination.target.name}")
            await asyncio.sleep(min(0.05, remaining))


@asynccontextmanager
async def run_environment_service(
    kubeconfig: Path,
    namespace: str,
    *,
    caller_uri: str,
    additional_callers: Sequence[str] = (),
    environment_runtime_ca: CertificateAuthority | None = None,
    runtime_proxy_readers: Sequence[str] = (APP_URI, GATEWAY_SESSIONS_URI),
    runtime_proxy_session_callers: Sequence[str] = (GATEWAY_SESSIONS_URI,),
    runtime_proxy_controller_callers: Sequence[str] = (),
    runtime_proxy_executor_callers: Sequence[str] = (),
) -> AsyncIterator[EnvironmentServiceProcess]:
    """Serve production methods and auth over a supplied Kubernetes API server.

    The Runtime proxy uses its dedicated service SVID to connect to each Sandbox Runtime.
    Its method grants are configurable so tests can opt into Controller or Executor operations.
    """
    runfiles = Runfiles.Create()
    assert runfiles is not None
    binary = runfiles.Rlocation(
        "ducktape/agentplane/environments/testing/serving_fixture/serving_fixture_/serving_fixture"
    )
    assert binary is not None
    with TemporaryDirectory(prefix="environments-test-") as work, listen_tcp() as listener:
        directory = Path(work)
        ca = environment_runtime_ca or make_ca("EnvironmentService fixture")
        ca_file, _ = write_ca(ca, directory, "ca")
        server_dir = directory / "server"
        server_dir.mkdir()
        cert, key = issue_workload_leaf(ca, SANDBOX_CONTROLLER_URI, server_dir, dns_name="localhost")

        service_callers = tuple(dict.fromkeys((caller_uri, *additional_callers)))
        runtime_callers = tuple(
            dict.fromkeys(
                (
                    *service_callers,
                    *runtime_proxy_readers,
                    *runtime_proxy_session_callers,
                    *runtime_proxy_controller_callers,
                    *runtime_proxy_executor_callers,
                    GATEWAY_RUNTIME_PROXY_URI,
                    SANDBOX_CONTROLLER_URI,
                )
            )
        )
        service_certificates: dict[str, tuple[Path, Path]] = {}
        for uri in runtime_callers:
            client_dir = directory / uri.rsplit("/", 1)[-1]
            client_dir.mkdir()
            service_certificates[uri] = issue_workload_leaf(ca, uri, client_dir)

        runtime_proxy_cert, runtime_proxy_key = service_certificates[GATEWAY_RUNTIME_PROXY_URI]
        credentials = grpc.ssl_channel_credentials(root_certificates=ca.cert_pem)
        environment = {
            **os.environ,
            "KUBECONFIG": str(kubeconfig),
            "ENVIRONMENTS_TEST_NAMESPACE": namespace,
            "ENVIRONMENTS_TEST_CALLERS": ",".join(service_callers),
            "ENVIRONMENTS_TEST_LISTEN_ADDRESS": f"fd:{listener.socket.fileno()}",
            "ENVIRONMENTS_TEST_CERT": str(cert),
            "ENVIRONMENTS_TEST_KEY": str(key),
            "ENVIRONMENTS_TEST_CA": str(ca_file),
            "ENVIRONMENTS_TEST_RUNTIME_PROXY_CERT": str(runtime_proxy_cert),
            "ENVIRONMENTS_TEST_RUNTIME_PROXY_KEY": str(runtime_proxy_key),
            "ENVIRONMENTS_TEST_RUNTIME_PROXY_CA": str(ca_file),
            "ENVIRONMENTS_TEST_RUNTIME_READERS": ",".join(runtime_proxy_readers),
            "ENVIRONMENTS_TEST_RUNTIME_SESSION_CALLERS": ",".join(runtime_proxy_session_callers),
            "ENVIRONMENTS_TEST_RUNTIME_CONTROLLER_CALLERS": ",".join(runtime_proxy_controller_callers),
            "ENVIRONMENTS_TEST_RUNTIME_EXECUTOR_CALLERS": ",".join(runtime_proxy_executor_callers),
        }

        def process_context(generation: int) -> AbstractAsyncContextManager[GoProcess]:
            log_path = undeclared_outputs_dir() / f"environments-{uuid4().hex}-{generation}.log"
            return go_process(
                Path(binary),
                env=environment,
                stderr=log_path,
                health_port=listener.port,
                health_credentials=credentials,
                pass_fds=(listener.socket.fileno(),),
                grace_s=10,
            )

        async with restartable_go_process(process_context) as (started, restart):
            yield EnvironmentServiceProcess(
                target=f"localhost:{listener.port}",
                client_uri=caller_uri,
                ca=ca,
                ca_file=ca_file,
                service_certificates=service_certificates,
                process=started.process,
                log_path=started.stderr,
                _restart=restart,
            )
