"""Run the built Policy Controller for consumer tests, serving PolicyService over the test API server's Identities.

The Component tier runs real neighbours (docs/test_tiers.md), so a Go service that resolves Identities through
PolicyService is tested against the controller that serves it in production. The controller also runs its
reconcilers, which act on any PolicyBindings, grants or accounts a test creates.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import yaml
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.config import new_client_from_config
from python.runfiles import Runfiles

from agentplane.net.peertls.config import (
    POLICY_CONTROLLER_URI,
    ClientTLS,
    PolicyServicePeer,
    ServerTLSListener,
    ServiceSPIFFEID,
)
from agentplane.policy_controller.settings import Settings
from agentplane.testing.go_process import HttpReadiness, go_process
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import listen_tcp

_BINARY = (
    Runfiles.CreateOrRaise().root() / "ducktape/agentplane/policy_controller/cmd/policy-controller_/policy-controller"
)
_GRACE_SECONDS = 10


@asynccontextmanager
async def policy_controller(
    kubeconfig: Path, *, policy_namespace: str, ca: CertificateAuthority, directory: Path, client_uri: ServiceSPIFFEID
) -> AsyncIterator[PolicyServicePeer]:
    """The Policy Controller's mutual-TLS PolicyService as `client_uri` reaches it, for the context's life.

    It reads the Identities and policy objects in `policy_namespace` of the API server `kubeconfig` reaches, and
    admits only `client_uri`, with certificates issued by `ca`. `directory` is created here and holds the
    certificates and config.
    """
    await _ensure_namespace(kubeconfig, policy_namespace)
    client_directory = directory / "client"
    await asyncio.to_thread(client_directory.mkdir, parents=True)
    server_cert, server_key = issue_workload_leaf(ca, POLICY_CONTROLLER_URI, directory, dns_name="localhost")
    client_cert, client_key = issue_workload_leaf(ca, client_uri, client_directory)
    ca_file, _ = write_ca(ca, directory, "ca")
    # The health server binds an address, not a descriptor, so reserve a port and release it just before it starts.
    with listen_tcp() as probe:
        probe_port = probe.port
    with listen_tcp() as grpc_listener:
        settings = Settings(
            policy_namespace=policy_namespace,
            subject_namespaces=frozenset({policy_namespace}),
            accounts_namespace=f"{policy_namespace}-accounts",
            metrics_bind_address="0",
            health_probe_bind_address=f"127.0.0.1:{probe_port}",
            grpc_tls=ServerTLSListener(
                listen=f"fd:{grpc_listener.socket.fileno()}",
                cert_file=server_cert,
                key_file=server_key,
                client_ca_file=ca_file,
            ),
            reader_services=frozenset({client_uri}),
        )
        config = directory / "policy-controller.yaml"
        config.write_text(yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True), sort_keys=False))
        async with go_process(
            _BINARY,
            env={Settings.config_file_env: str(config), "KUBECONFIG": str(kubeconfig)},
            stderr=undeclared_outputs_dir() / f"policy-controller-{uuid4().hex}.log",
            readiness=HttpReadiness(url=f"http://127.0.0.1:{probe_port}/readyz"),
            grace_s=_GRACE_SECONDS,
            pass_fds=(grpc_listener.socket.fileno(),),
        ):
            yield PolicyServicePeer(
                target=f"localhost:{grpc_listener.port}",
                tls=ClientTLS(
                    ca_file=ca_file,
                    server_uri=POLICY_CONTROLLER_URI,
                    cert_file=client_cert,
                    key_file=client_key,
                    client_uri=client_uri,
                ),
            )


async def _ensure_namespace(kubeconfig: Path, namespace: str) -> None:
    """The controller's leader-election Lease lives here, so it must exist when the controller starts."""
    async with await new_client_from_config(config_file=str(kubeconfig)) as api:
        try:
            await k8s_client.CoreV1Api(api).create_namespace(
                k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace))
            )
        except k8s_client.ApiException as error:
            if error.status != 409:
                raise
