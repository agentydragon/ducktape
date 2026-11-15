"""Run the production EnvironmentService listener against an envtest API server.

The app fixtures control Sandbox status and Runtime processes themselves; this runs the exact
Controller serving component, without a competing reconciler changing those test resources.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import grpc
from python.runfiles import Runfiles

from agentplane.net.peertls.config import SANDBOX_CONTROLLER_URI, ClientTLS
from agentplane.testing.go_process import go_process
from agentplane.testing.tls import issue_workload_leaf, make_ca, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import listen_tcp


@dataclass(frozen=True)
class EnvironmentServiceProcess:
    target: str
    tls: ClientTLS


@asynccontextmanager
async def run_environment_service(
    kubeconfig: Path, namespace: str, *, caller_uri: str
) -> AsyncIterator[EnvironmentServiceProcess]:
    runfiles = Runfiles.Create()
    assert runfiles is not None
    binary = runfiles.Rlocation("_main/agentplane/environments/testing/serving_fixture/serving_fixture")
    assert binary is not None
    with TemporaryDirectory(prefix="environments-test-") as work, listen_tcp() as listener:
        directory = Path(work)
        ca = make_ca("EnvironmentService fixture")
        ca_file, _ = write_ca(ca, directory, "ca")
        server_dir = directory / "server"
        server_dir.mkdir()
        cert, key = issue_workload_leaf(ca, SANDBOX_CONTROLLER_URI, server_dir, dns_name="localhost")
        client_dir = directory / "client"
        client_dir.mkdir()
        client_cert, client_key = issue_workload_leaf(ca, caller_uri, client_dir)
        async with go_process(
            Path(binary),
            env={
                **os.environ,
                "KUBECONFIG": str(kubeconfig),
                "ENVIRONMENTS_TEST_NAMESPACE": namespace,
                "ENVIRONMENTS_TEST_CALLER_URI": caller_uri,
                "ENVIRONMENTS_TEST_LISTEN_ADDRESS": f"fd:{listener.socket.fileno()}",
                "ENVIRONMENTS_TEST_CERT": str(cert),
                "ENVIRONMENTS_TEST_KEY": str(key),
                "ENVIRONMENTS_TEST_CA": str(ca_file),
            },
            stderr=undeclared_outputs_dir() / f"environments-{uuid4().hex}.log",
            health_port=listener.port,
            health_credentials=grpc.ssl_channel_credentials(root_certificates=ca.cert_pem),
            pass_fds=(listener.socket.fileno(),),
            grace_s=10,
        ):
            yield EnvironmentServiceProcess(
                target=f"localhost:{listener.port}",
                tls=ClientTLS(
                    ca_file=ca_file,
                    server_uri=SANDBOX_CONTROLLER_URI,
                    cert_file=client_cert,
                    key_file=client_key,
                    client_uri=caller_uri,
                ),
            )
