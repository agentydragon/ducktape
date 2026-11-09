"""SVID verifiers trust the Agentplane workload CA alone, not the cluster root it chains to.

A Certificate anyone may request from `cluster-internal-ca`, naming a live Sandbox's exact Runtime
URI, its WorkloadCredential's, with client auth, must not authenticate to the egress proxy's forward
listener; one naming the Runner Gateway must not authenticate to a runner's control listener; one
naming a runner, served at that runner's address, must not authenticate to the Gateway dialing it.
The real credential, presented the same way, is the positive anchor: it shows the probe reaches the
verifier and that only the issuer differs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import ssl
import tempfile
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import yaml

from agentplane.crd_group import GROUP
from agentplane.environments.v1 import environment_service_pb2
from agentplane.sandbox_resource import SANDBOX_API_VERSION
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.port_forward import PortForward, async_port_forward
from agentplane.system_tests.driver.sandbox import ENVIRONMENT_RUNTIME_CONTAINER, POD_TEMPLATE
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


async def _cluster_ca_leaf(
    cluster: Cluster, uri: str, usages: Sequence[str] = ("digital signature", "client auth")
) -> tuple[bytes, bytes]:
    """A leaf naming `uri` that `cluster-internal-ca` signs, requested in a fresh namespace."""
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
                    "usages": list(usages),
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
    # The Sandbox's WorkloadCredential names the Runtime's URI and the Secret holding its key pair.
    record = json.loads(await cluster.kubectl("get", "workloadcredential", view.target.name, "-o", "json"))
    credential, uri = record["status"]["secretName"], record["status"]["uri"]

    impostor = await _cluster_ca_leaf(cluster, uri)
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
    impostor = await _cluster_ca_leaf(cluster, _GATEWAY_URI)
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


# Where the impostor Sandbox's Runtime reads its serving certificate: a Secret volume, which kubelet
# updates in place and the Runtime reloads on its next handshake.
_SERVING_LEAF_PATH = "/run/agentplane/impostor-leaf"
_RUNTIME_CONFIG_VOLUME = "environment-runtime-config"
_RUNTIME_CONFIG_KEY = "environment-runtime.json"


async def _object(cluster: Cluster, kind: str, name: str) -> dict:
    decoded = json.loads(await cluster.kubectl("get", kind, name, "-o", "json"))
    assert isinstance(decoded, dict)
    return decoded


async def _sandbox_status(cluster: Cluster, name: str, *path: str) -> object:
    """Poll the Sandbox until its status holds `path`, and return that value."""
    async with asyncio.timeout(READY_SECONDS):
        while True:
            value: object = (await _object(cluster, f"sandboxes.{GROUP}", name)).get("status", {})
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if value:
                return value
            await asyncio.sleep(1)


async def _put_tls_secret(cluster: Cluster, name: str, crt: bytes, key: bytes) -> None:
    await cluster.kubectl(
        "apply",
        "-f",
        "-",
        body={
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": cluster.namespace},
            "type": "kubernetes.io/tls",
            "data": {"tls.crt": base64.b64encode(crt).decode(), "tls.key": base64.b64encode(key).decode()},
        },
    )


async def _gateway_handshake_failures(cluster: Cluster, sandbox: str) -> list[str]:
    """The errors of the Gateway's failed attempts to observe `sandbox`'s runner."""
    logs = await cluster.kubectl(
        "logs", "-l", "app.kubernetes.io/name=runner-gateway", "--tail=-1", "--since=10m", "--prefix=false"
    )
    failures = []
    for line in logs.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            record = json.loads(line)
            if record.get("msg") == "runner observer reconnecting" and record.get("sandbox") == sandbox:
                failures.append(record["error"])
    return failures


async def a_cluster_ca_leaf_at_a_runner_address_is_refused_by_the_gateway(
    cluster: Cluster, view: environment_service_pb2.EnvironmentDescriptor
) -> None:
    """A Sandbox whose own Runtime serves a `cluster-internal-ca` leaf naming its runner URI: the
    Gateway dials it at the runner's Pod address and fails the TLS handshake, and the Sandbox is not
    Ready. Swapping the same file to the Sandbox's own credential makes it Ready, so the Gateway was
    dialing that address and only the issuer differed. The real runner `view` stays connected."""
    name = f"systest-impostor-{uuid4().hex[:8]}"
    base = await _object(cluster, f"sandboxtemplates.{GROUP}", POD_TEMPLATE)
    spec = copy.deepcopy(base["spec"])
    pod = spec["pod"]["spec"]
    config_volume = next(v for v in pod["volumes"] if v["name"] == _RUNTIME_CONFIG_VOLUME)
    base_config = await _object(cluster, "configmap", config_volume["configMap"]["name"])
    runtime_config = json.loads(base_config["data"][_RUNTIME_CONFIG_KEY])
    # The cluster bundle still verifies the Runtime's own chain, so it starts with the impostor leaf.
    runtime_config["control"]["security"]["tls"] |= {
        "cert_file": f"{_SERVING_LEAF_PATH}/tls.crt",
        "key_file": f"{_SERVING_LEAF_PATH}/tls.key",
    }
    config_volume["configMap"]["name"] = name
    runtime = next(c for c in pod["containers"] if c["name"] == ENVIRONMENT_RUNTIME_CONTAINER)
    runtime["volumeMounts"] = [
        *runtime.get("volumeMounts", []),
        {"name": "impostor-leaf", "mountPath": _SERVING_LEAF_PATH, "readOnly": True},
    ]
    # Kubelet holds the Pod until the Secret exists, which needs the URI the controller allocates.
    pod["volumes"].append({"name": "impostor-leaf", "secret": {"secretName": name}})
    gateway_settings = yaml.safe_load(
        (await _object(cluster, "configmap", "runner-gateway-settings"))["data"]["settings.yaml"]
    )
    async with contextlib.AsyncExitStack() as cleanup:
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": name, "namespace": cluster.namespace},
                "data": {_RUNTIME_CONFIG_KEY: json.dumps(runtime_config)},
            },
        )
        cleanup.push_async_callback(cluster.kubectl, "delete", "configmap", name, "--wait=false")
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": SANDBOX_API_VERSION,
                "kind": "SandboxTemplate",
                "metadata": {"name": name, "namespace": cluster.namespace},
                "spec": spec,
            },
        )
        cleanup.push_async_callback(cluster.kubectl, "delete", f"sandboxtemplates.{GROUP}", name, "--wait=false")
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            body={
                "apiVersion": SANDBOX_API_VERSION,
                "kind": "Sandbox",
                "metadata": {"name": name, "namespace": cluster.namespace},
                "spec": {
                    "template": name,
                    "running": True,
                    "policySets": gateway_settings["environment_default_policy_sets"],
                },
            },
        )
        cleanup.push_async_callback(cluster.kubectl, "delete", "secret", name, "--wait=false")
        cleanup.push_async_callback(cluster.kubectl, "delete", f"sandboxes.{GROUP}", name, "--timeout=300s")
        # The Sandbox's WorkloadCredential, once issued, names the Runtime's URI and its own key pair's Secret.
        async with asyncio.timeout(READY_SECONDS):
            while True:
                with contextlib.suppress(RuntimeError):
                    status = (await _object(cluster, "workloadcredential", name)).get("status", {})
                    if status.get("uri") and status.get("secretName"):
                        break
                await asyncio.sleep(1)
        uri, own_secret = status["uri"], status["secretName"]
        impostor = await _cluster_ca_leaf(cluster, uri, ("digital signature", "server auth", "client auth"))
        await _put_tls_secret(cluster, name, *impostor)

        async with asyncio.timeout(READY_SECONDS):
            while True:
                failures = await _gateway_handshake_failures(cluster, name)
                if any("authentication handshake failed" in error for error in failures):
                    break
                await asyncio.sleep(2)
        sandbox = await _object(cluster, f"sandboxes.{GROUP}", name)
        ready = {c["type"]: c["status"] for c in sandbox["status"].get("conditions", [])}.get("Ready")
        assert ready != "True", f"a Sandbox serving a cluster-internal-ca leaf naming {uri} is Ready"

        await _put_tls_secret(cluster, name, *await _secret_pem(cluster, cluster.namespace, own_secret))
        await cluster.kubectl("wait", f"sandboxes.{GROUP}/{name}", "--for=condition=Ready=True", "--timeout=300s")
    # The Gateway's real runner connection still holds: a Sandbox is Ready only while its runner is connected.
    await cluster.kubectl(
        "wait",
        f"sandbox.agentplane.allegedly.works/{view.target.name}",
        "--for=condition=Ready=True",
        f"--timeout={READY_SECONDS}s",
    )
