"""SVID verifiers trust the Agentplane workload CA alone, not the cluster root it chains to.

A Certificate anyone may request from `cluster-internal-ca`, naming a live Sandbox's exact Runtime
URI with client auth, must not authenticate to the egress proxy's forward listener; one naming the
Runner Gateway must not authenticate to a runner's control listener. The real credential, presented
the same way, is the positive anchor: it shows the probe reaches the listener and that only the
issuer differs.
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
# cluster/cdk8s/agentplane/runner_gateway.py `GATEWAY_TLS_SECRET`, and the runner's control port.
_GATEWAY_SECRET = "agentplane-runner-gateway-tls"
_GATEWAY_URI = "spiffe://agentplane/runner-gateway"
_RUNNER_CONTROL_PORT = 9090
_HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
# An empty SETTINGS frame: length 0, type 0x4, no flags, stream 0.
_HTTP2_EMPTY_SETTINGS = b"\x00\x00\x00\x04\x00\x00\x00\x00\x00"
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


async def _cluster_ca_client_leaf(cluster: Cluster, uri: str) -> tuple[bytes, bytes]:
    """A client-auth leaf naming `uri` that `cluster-internal-ca` signs, requested in a fresh namespace."""
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
                "metadata": {"name": "foreign-leaf", "namespace": foreign},
                "spec": {
                    "secretName": "foreign-leaf",
                    "issuerRef": {"name": "cluster-internal-ca", "kind": "ClusterIssuer"},
                    "uris": [uri],
                    "usages": ["digital signature", "client auth"],
                    "privateKey": {"algorithm": "ECDSA", "size": 256},
                },
            },
        )
        await cluster.kubectl(
            "wait", "-n", foreign, "--for=condition=Ready", "certificate/foreign-leaf", f"--timeout={READY_SECONDS}s"
        )
        return await _secret_pem(cluster, foreign, "foreign-leaf")
    finally:
        await cluster.kubectl("delete", "namespace", foreign, "--wait=false")


async def a_cluster_ca_leaf_with_a_live_sandbox_uri_is_refused(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> None:
    sandbox = json.loads(await cluster.kubectl("get", "sandbox", view.target.name, "-o", "json"))
    credential = f"runner-{sandbox['status']['runnerCredential']['id']}"
    runtime = json.loads(await cluster.kubectl("get", "certificate", credential, "-o", "json"))
    (uri,) = runtime["spec"]["uris"]

    impostor = await _cluster_ca_client_leaf(cluster, uri)
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


async def _answers_http2(target: str, *, roots: bytes, certificate: tuple[bytes, bytes]) -> bool:
    """Whether the runner's gRPC control listener answers an HTTP/2 preface from a client presenting
    `certificate` with its own SETTINGS frame.

    The runner's SVID carries only a URI SAN, so hostname checking is off; its chain is still
    verified against `roots`. Under TLS 1.3 a refused client certificate surfaces on the first read.
    """
    context = ssl.create_default_context(cadata=roots.decode())
    context.check_hostname = False
    context.set_alpn_protocols(["h2"])
    with tempfile.TemporaryDirectory(prefix="workload-trust-") as work:
        cert_file, key_file = Path(work) / "tls.crt", Path(work) / "tls.key"
        cert_file.write_bytes(certificate[0])
        key_file.write_bytes(certificate[1])
        context.load_cert_chain(cert_file, key_file)
    host, port = target.rsplit(":", 1)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port), ssl=context), READY_SECONDS)
    except ssl.SSLError, ConnectionResetError:
        return False
    try:
        writer.write(_HTTP2_PREFACE + _HTTP2_EMPTY_SETTINGS)
        await writer.drain()
        try:
            header = await asyncio.wait_for(reader.readexactly(9), READY_SECONDS)
        except ssl.SSLError, ConnectionResetError, asyncio.IncompleteReadError:
            return False
        # Frame type 0x4: the server's SETTINGS, the first frame an HTTP/2 server sends.
        return header[3] == 0x4
    finally:
        # Abort rather than race TLS shutdown against frames the server is still sending.
        writer.transport.abort()
        with contextlib.suppress(ssl.SSLError, ConnectionResetError):
            await writer.wait_closed()


async def a_cluster_ca_leaf_naming_the_gateway_is_refused_by_the_runner(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> None:
    impostor = await _cluster_ca_client_leaf(cluster, _GATEWAY_URI)
    gateway = await _secret_pem(cluster, cluster.namespace, _GATEWAY_SECRET)
    roots = await service_root_certificates(cluster)
    forward = PortForward(
        cluster.command("port-forward"),
        resource=f"pod/{view.kubernetes_diagnostics.pod.name}",
        local_address="127.0.0.1",
        local_port=None,
        remote_port=_RUNNER_CONTROL_PORT,
    )
    async with async_port_forward(forward) as target:
        assert await _answers_http2(target, roots=roots, certificate=gateway), "the Gateway's own SVID was refused"
        assert not await _answers_http2(target, roots=roots, certificate=impostor), (
            f"a cluster-internal-ca leaf naming {_GATEWAY_URI} authenticated to a runner's control listener"
        )
    # The Gateway's own connection still holds: a Sandbox is Ready only while its runner is connected.
    await cluster.kubectl(
        "wait",
        f"sandbox.agentplane.allegedly.works/{view.target.name}",
        "--for=condition=Ready=True",
        f"--timeout={READY_SECONDS}s",
    )
