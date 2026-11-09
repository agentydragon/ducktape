"""SVID verifiers trust the Agentplane workload CA alone, not the cluster root it chains to.

A Certificate anyone may request from `cluster-internal-ca`, naming a live Sandbox's exact Runtime
URI with client auth, must not authenticate to the egress proxy's forward listener. The Sandbox's own
credential, presented the same way, is the positive anchor: it shows the probe reaches the listener
and that only the issuer differs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import ssl
import tempfile
from pathlib import Path
from uuid import uuid4

from agentplane.environments.v1 import environment_service_pb2
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.tls import service_root_certificates

# cluster/cdk8s/agentplane/egress.py: `proxy()` and its listener port.
_PROXY_SERVICE = "service/egress"
_PROXY_PORT = 8888
READY_SECONDS = 120

# `environment_service_pb2.pyi` imports google.protobuf, which mypy follows for this direct dependency.
# gazelle:include_dep @pypi//protobuf


async def _secret_pem(cluster: Cluster, namespace: str, name: str) -> tuple[bytes, bytes]:
    secret = json.loads(await cluster.kubectl("get", "secret", name, "-n", namespace, "-o", "json"))
    return base64.b64decode(secret["data"]["tls.crt"]), base64.b64decode(secret["data"]["tls.key"])


async def _answers_connect(
    target: str, *, roots: bytes, server_name: str, certificate: tuple[bytes, bytes], authority: str
) -> bool:
    """Whether the forward listener answers a CONNECT from a client presenting `certificate`.

    Under TLS 1.3 the server refuses a client certificate after the client's handshake completes,
    so the refusal surfaces on the first read.
    """
    context = ssl.create_default_context(cadata=roots.decode())
    with tempfile.TemporaryDirectory(prefix="workload-trust-") as work:
        cert_file, key_file = Path(work) / "tls.crt", Path(work) / "tls.key"
        cert_file.write_bytes(certificate[0])
        key_file.write_bytes(certificate[1])
        context.load_cert_chain(cert_file, key_file)
    host, port = target.rsplit(":", 1)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port), ssl=context, server_hostname=server_name), READY_SECONDS
        )
    except ssl.SSLError, ConnectionResetError:
        return False
    try:
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await writer.drain()
        try:
            status = await asyncio.wait_for(reader.readline(), READY_SECONDS)
        except ssl.SSLError, ConnectionResetError:
            return False
        return status.startswith(b"HTTP/1.1 ")
    finally:
        writer.close()
        # Closing a connection the server already refused reports that refusal again.
        with contextlib.suppress(ssl.SSLError, ConnectionResetError):
            await writer.wait_closed()


async def a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> None:
    sandbox = json.loads(await cluster.kubectl("get", "sandbox", view.target.name, "-o", "json"))
    credential = f"runner-{sandbox['status']['runnerCredential']['id']}"
    runtime = json.loads(await cluster.kubectl("get", "certificate", credential, "-o", "json"))
    (uri,) = runtime["spec"]["uris"]

    foreign = f"systest-foreign-{uuid4().hex[:8]}"
    await cluster.kubectl("create", "namespace", foreign)
    try:
        await cluster.kubectl(
            "apply",
            "-n",
            foreign,
            "-f",
            "-",
            body={
                "apiVersion": "cert-manager.io/v1",
                "kind": "Certificate",
                "metadata": {"name": "foreign-runtime", "namespace": foreign},
                "spec": {
                    "secretName": "foreign-runtime",
                    "issuerRef": {"name": "cluster-internal-ca", "kind": "ClusterIssuer"},
                    "uris": [uri],
                    "usages": ["digital signature", "client auth"],
                    "privateKey": {"algorithm": "ECDSA", "size": 256},
                },
            },
        )
        await cluster.kubectl(
            "wait", "-n", foreign, "--for=condition=Ready", "certificate/foreign-runtime", f"--timeout={READY_SECONDS}s"
        )
        impostor = await _secret_pem(cluster, foreign, "foreign-runtime")
    finally:
        await cluster.kubectl("delete", "namespace", foreign, "--wait=false")
    own = await _secret_pem(cluster, cluster.namespace, credential)

    roots = await service_root_certificates(cluster)
    forward = PortForward(
        cluster.command("port-forward"),
        resource=_PROXY_SERVICE,
        local_address="127.0.0.1",
        local_port=None,
        remote_port=_PROXY_PORT,
    )
    # The k3s fixture adds this DNS name to the proxy's SVID for Python clients.
    server_name = f"egress-admin.{cluster.namespace}.svc.cluster.local"
    authority = f"llm-ingress.{cluster.namespace}.svc.cluster.local:8080"
    async with async_port_forward(forward) as target:

        async def answers(certificate: tuple[bytes, bytes]) -> bool:
            return await _answers_connect(
                target, roots=roots, server_name=server_name, certificate=certificate, authority=authority
            )

        assert await answers(own), "the Sandbox's own SVID was refused"
        assert not await answers(impostor), f"a cluster-internal-ca leaf naming {uri} authenticated to the egress proxy"
