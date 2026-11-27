"""Run the built Policy Controller for consumer tests, serving PolicyService over the test API server's Identities.

The Component tier runs real neighbours (docs/test_tiers.md), so a Go service that resolves Identities through
PolicyService is tested against the controller that serves it in production. The controller also runs its
reconcilers, which act on any PolicyBindings, grants or accounts a test creates.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import yaml
from kubernetes_asyncio import client as k8s_client
from python.runfiles import Runfiles

from agentplane.net.peertls.config import POLICY_CONTROLLER_URI, ServerTLSListener
from agentplane.policy_controller.settings import Settings
from agentplane.testing.go_process import HttpReadiness, go_process
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer
from agentplane.testing.tls import CertificateAuthority, issue_workload_leaf, write_ca
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.net import listen_tcp

_BINARY = (
    Runfiles.CreateOrRaise().root() / "ducktape/agentplane/policy_controller/cmd/policy-controller_/policy-controller"
)
_GRACE_SECONDS = 10


@asynccontextmanager
async def policy_controller(
    api: KubernetesApiServer,
    *,
    policy_namespace: str,
    ca: CertificateAuthority,
    directory: Path,
    readers: Collection[str],
) -> AsyncIterator[str]:
    """The `host:port` of the Policy Controller's mutual-TLS PolicyService, for the context's life.

    It reads `api`'s Identities and policy objects in `policy_namespace`, and admits the service SPIFFE IDs in
    `readers`, authenticated against `ca`. `directory` is created here and holds its certificates and config.
    """
    await _ensure_namespace(api, policy_namespace)
    await asyncio.to_thread(directory.mkdir)
    cert_file, key_file = issue_workload_leaf(ca, POLICY_CONTROLLER_URI, directory, dns_name="localhost")
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
                cert_file=cert_file,
                key_file=key_file,
                client_ca_file=ca_file,
            ),
            reader_services=frozenset(readers),
        )
        config = directory / "policy-controller.yaml"
        config.write_text(yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True), sort_keys=False))
        async with go_process(
            _BINARY,
            env={Settings.config_file_env: str(config), "KUBECONFIG": str(api.kubeconfig)},
            stderr=undeclared_outputs_dir() / f"policy-controller-{uuid4().hex}.log",
            readiness=HttpReadiness(url=f"http://127.0.0.1:{probe_port}/readyz"),
            grace_s=_GRACE_SECONDS,
            pass_fds=(grpc_listener.socket.fileno(),),
        ):
            yield f"localhost:{grpc_listener.port}"


async def _ensure_namespace(api: KubernetesApiServer, namespace: str) -> None:
    """The controller's leader-election Lease lives here, so it must exist when the controller starts."""
    try:
        await k8s_client.CoreV1Api(api.api).create_namespace(
            k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace))
        )
    except k8s_client.ApiException as error:
        if error.status != 409:
            raise
