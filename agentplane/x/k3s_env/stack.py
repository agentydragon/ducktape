"""A bare-k3s copy of the `agentplane-testing` stack on one RBE action, for tests to drive.

Starts k3s with kube-proxy and no Cilium, loads the Bazel-built service images, applies the composed
manifest and yields the app's address and a token once every Deployment is Available. It fails at the
first Deployment that does not, with the Pod states and logs of whatever did not start.

The test process reaches the app (and Dex) by their in-cluster Service names, which it resolves
through `/etc/hosts`: OIDC redirects name them, and the action is discarded after the test. Dex's
certificate comes from a CA made per stack, which `McpFixtureAccess.ca_bundle` adds to the system roots.
The Action Service's OAuth server names itself by a loopback origin, which a port-forward serves.
"""

import contextlib
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from agentplane.acceptance.conftest import AUDIENCE, TESTING_SERVICE_ACCOUNT
from agentplane.testing import tls
from agentplane.x.k3s_env import rendered_fixture
from agentplane.x.k3s_env.bootstrap import Bootstrap
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 480
BOOTSTRAP_TIMEOUT_SECONDS = 180
# k3s's default Pod CIDR.
POD_CIDR = "10.42.0.0/16"


def run(*command: str, check: bool = True, input_text: str | None = None) -> str:
    result = subprocess.run(command, capture_output=True, text=True, input=input_text, check=False)
    if check and result.returncode:
        raise RuntimeError(
            f"{command[:3]} exited {result.returncode}\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )
    return result.stdout + result.stderr


def _shadow_kernel_sysctls(work: Path) -> None:
    """The Firecracker guest has no CONFIG_KEYS; the kubelet refuses to start without these files."""
    if Path("/proc/sys/kernel/keys/root_maxkeys").exists():
        return
    fake = work / "proc-sys-kernel"
    fake.mkdir()
    for path in Path("/proc/sys/kernel").rglob("*"):
        target = fake / path.relative_to("/proc/sys/kernel")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            # The kubelet opens some sysctls (panic, osrelease) even when they cannot be read, so an
            # unreadable one is shadowed by an empty file.
            target.touch()
            with contextlib.suppress(OSError):
                target.write_bytes(path.read_bytes())
    (fake / "keys").mkdir(exist_ok=True)
    (fake / "keys/root_maxkeys").write_text("1000000\n")
    (fake / "keys/root_maxbytes").write_text("25000000\n")
    run("mount", "--bind", str(fake), "/proc/sys/kernel")


def _archive_json(archive: tarfile.TarFile, name: str, value: object) -> None:
    encoded = json.dumps(value).encode()
    entry = tarfile.TarInfo(name)
    entry.size = len(encoded)
    archive.addfile(entry, io.BytesIO(encoded))


def node_ip() -> str:
    """The address Pods use to reach host-side processes: the apiserver, the cluster DNS, a test's
    scripted model."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        return str(probe.getsockname()[0])


class K3s:
    def __init__(self, work: Path, binary: Path) -> None:
        self.work = work
        self.binary = binary
        self.kubeconfig = work / "kubeconfig"
        self.data_dir = Path("/var/lib/k3s-env")
        self.node_ip = node_ip()

    def kubectl(self, *args: str, check: bool = True, input_text: str | None = None) -> str:
        return run(
            str(self.binary), "kubectl", "--kubeconfig", str(self.kubeconfig), *args, check=check, input_text=input_text
        )

    def start(self) -> subprocess.Popen[bytes]:
        _shadow_kernel_sysctls(self.work)
        self.log_path = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", self.work)) / "k3s.log"
        log = self.log_path.open("wb")
        process = subprocess.Popen(
            [
                str(self.binary),
                "server",
                "--data-dir",
                str(self.data_dir),
                "--write-kubeconfig",
                str(self.kubeconfig),
                "--write-kubeconfig-mode",
                "644",
                "--node-name",
                "env",
                "--disable",
                "traefik,metrics-server,servicelb",
                "--flannel-backend=host-gw",
                "--disable-network-policy",
                "--node-ip",
                self.node_ip,
                # The runner Pod mounts the runner disk image as an image volume (beta, off by default in 1.34).
                "--kube-apiserver-arg",
                "feature-gates=ImageVolume=true",
                "--kubelet-arg",
                "feature-gates=ImageVolume=true",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        bootstrap = Bootstrap(
            process=process,
            binary=self.binary,
            kubeconfig=self.kubeconfig,
            log_path=self.log_path,
            deadline=time.monotonic() + BOOTSTRAP_TIMEOUT_SECONDS,
        )
        try:
            bootstrap.wait_for(
                "get", "node/env", "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}", expected="True"
            )
            # host-gw routes Pod traffic but does not translate it: without this, Pods cannot reach the internet.
            iptables = self.data_dir / "data/current/bin/aux/iptables"  # the copy k3s ships; the host has none
            run(
                str(iptables), "-t", "nat", "-A", "POSTROUTING", "-s", POD_CIDR, "!", "-d", POD_CIDR, "-j", "MASQUERADE"
            )
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        return process

    def import_images(self, references: list[Path], *, required_images: set[str]) -> None:
        """Stream Bazel OCI layouts to containerd, preserving blob digests and sharing duplicate blobs."""
        if not references:
            raise ValueError("No Bazel OCI layouts found in runfiles")
        manifests = []
        blobs: dict[str, Path] = {}
        imported_tags: set[str] = set()
        for reference in references:
            layout = get_required_path(reference.read_text().strip())
            name = reference.stem.removesuffix("_layout")
            repository = "runner" if name == "runner" else "agentplane-" + name.replace("_", "-")
            tag = f"git.allegedly.works/ducktape-ci/{repository}:unset"
            imported_tags.add(tag)
            index = json.loads((layout / "index.json").read_text())
            for descriptor in index["manifests"]:
                descriptor.setdefault("annotations", {})["org.opencontainers.image.ref.name"] = tag
                manifests.append(descriptor)
            for blob in (layout / "blobs").glob("*/*"):
                blobs.setdefault(blob.relative_to(layout).as_posix(), blob)
        if missing := required_images - imported_tags:
            raise ValueError(f"Fixture workloads lack OCI layouts for: {sorted(missing)}")
        print(f"importing {len(references)} OCI layouts with {len(blobs)} distinct blobs")
        with tempfile.TemporaryFile() as output:
            with subprocess.Popen(
                [
                    str(self.binary),
                    "ctr",
                    "--address",
                    "/run/k3s/containerd/containerd.sock",
                    "-n",
                    "k8s.io",
                    "images",
                    "import",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
            ) as loader:
                assert loader.stdin is not None
                try:
                    with loader.stdin, tarfile.open(fileobj=loader.stdin, mode="w|", dereference=True) as archive:
                        _archive_json(archive, "oci-layout", {"imageLayoutVersion": "1.0.0"})
                        _archive_json(archive, "index.json", {"schemaVersion": 2, "manifests": manifests})
                        for name, blob in sorted(blobs.items()):
                            archive.add(blob, arcname=name, recursive=False)
                except BrokenPipeError:
                    loader.wait()
                    output.seek(0)
                    logger.exception("containerd closed its import stream: %s", output.read().decode()[-6000:])
                    raise
                code = loader.wait()
            output.seek(0)
            log = output.read().decode()
            if code:
                raise RuntimeError(f"containerd image import exited {code}: {log[-6000:]}")
            print(log)

    def report(self, namespace: str) -> str:
        parts = [
            self.kubectl("get", "pods", "-A", "-o", "wide", check=False),
            self.kubectl("get", "events", "-A", "--sort-by=.lastTimestamp", check=False)[-6000:],
        ]
        pods = self.kubectl("get", "pods", "-n", namespace, "-o", "name", check=False).split()
        for pod in pods:
            ready = self.kubectl(
                "get",
                pod,
                "-n",
                namespace,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                check=False,
            )
            if ready != "True":
                logs = self.kubectl("logs", pod, "-n", namespace, "--all-containers", "--tail=40", check=False)
                previous = self.kubectl(
                    "logs", pod, "-n", namespace, "--all-containers", "--previous", "--tail=40", check=False
                )
                parts.append(f"--- {pod}\n{logs}\n--- previous\n{previous}")
        return "\n".join(parts)


def _openssl_ca(work: Path) -> tuple[str, str]:
    key, cert = work / "ca.key", work / "ca.crt"
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    run(
        "openssl",
        "req",
        "-x509",
        "-new",
        "-key",
        str(key),
        "-days",
        "3",
        "-subj",
        "/CN=agentplane-egress-root-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-out",
        str(cert),
    )
    return cert.read_text(), key.read_text()


@dataclass(frozen=True)
class McpFixtureAccess:
    # The system roots and the CA of Dex's certificate.
    ca_bundle: Path
    # The Secret with the operator's Dex login.
    operator_secret: str
    # The Action Service's MCP endpoint, at the origin its OAuth server names itself by.
    actions_mcp_url: str


@dataclass(frozen=True)
class Stack:
    k3s: K3s
    namespace: str
    base_url: str
    token: str
    mcp_fixtures: McpFixtureAccess | None


def _mcp_inputs(work: Path, dex_host: str) -> rendered_fixture.McpInputs:
    ca = tls.make_ca("test-k3s-fixture-ca")
    cert, key = tls.issue_leaf(ca, dex_host, work)
    # A free port now; the port-forward listens on it once the Action Service is Available.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return rendered_fixture.McpInputs(
        dex_ca_cert=ca.cert_pem.decode(),
        dex_cert=cert.read_text(),
        dex_key=key.read_text(),
        actions_origin=f"http://127.0.0.1:{port}",
    )


@contextlib.contextmanager
def _port_forward(k3s: K3s, namespace: str, service: str, origin: str, port: int) -> Iterator[None]:
    """Forward the loopback `origin` to `service`'s `port` from when kubectl says it listens."""
    local = urlsplit(origin)
    with subprocess.Popen(
        [
            *(str(k3s.binary), "kubectl", "--kubeconfig", str(k3s.kubeconfig), "port-forward", "-n", namespace),
            *(f"--address={local.hostname}", f"service/{service}", f"{local.port}:{port}"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as forward:
        try:
            assert forward.stdout is not None
            if not (line := forward.stdout.readline()).startswith("Forwarding from"):
                raise RuntimeError(f"kubectl port-forward {service}: {line!r}")
            # Its log of each connection, which would fill the pipe unread.
            threading.Thread(target=shutil.copyfileobj, args=(forward.stdout, sys.stdout), daemon=True).start()
            yield
        finally:
            forward.terminate()


def _resolve_on_node(k3s: K3s, fixture: rendered_fixture.RenderedFixture) -> None:
    """Resolve the Services a browser reaches by name for this process, as the cluster DNS does for Pods."""
    with Path("/etc/hosts").open("a") as hosts:
        # The RBE executor's file ends without a newline.
        hosts.write("\n")
        for service in fixture.browser_services:
            address = k3s.kubectl(
                "get", "service", "-n", fixture.namespace, service.name, "-o", "jsonpath={.spec.clusterIP}"
            )
            hosts.write(f"{address} {service.host} {service.fqdn}\n")


def _deployment_images(objects: list[dict[str, Any]]) -> set[str]:
    return {
        container["image"]
        for doc in objects
        if doc["kind"] == "Deployment"
        for container in (
            doc["spec"]["template"]["spec"].get("initContainers", []) + doc["spec"]["template"]["spec"]["containers"]
        )
        if container["image"].startswith("git.allegedly.works/ducktape-ci/")
    }


@contextlib.contextmanager
def running_stack(*, models: rendered_fixture.Models) -> Iterator[Stack]:
    """The stack with every Deployment Available, LLM ingress forwarding to `models`.

    The manifest is the build's rendering for `models`' backend, `K3S_FIXTURE_RLOCATION`. A rendering
    with the MCP fixtures takes their image layouts from `K3S_MCP_FIXTURE_LAYOUTS`, space-separated
    runfiles paths of `.rloc` files.
    """
    crds = sorted(get_required_path("_main/agentplane/crds/manifests").glob("crd-*.yaml"))
    fixture = rendered_fixture.read(get_required_path(os.environ["K3S_FIXTURE_RLOCATION"]))
    layouts = sorted(get_required_path("_main/agentplane/x/k3s_env").glob("*_layout.rloc"))
    layouts.append(get_required_path(os.environ["K3S_RUNNER_LAYOUT_RLOCATION"]))
    if fixture.mcp_fixtures is not None:
        layouts.extend(get_required_path(path) for path in os.environ["K3S_MCP_FIXTURE_LAYOUTS"].split())

    work = Path(tempfile.mkdtemp())
    k3s = K3s(work, get_required_path(os.environ["K3S_BINARY_RLOCATION"]))
    process = k3s.start()
    with contextlib.ExitStack() as cleanup:
        cleanup.callback(shutil.rmtree, work, ignore_errors=True)
        cleanup.callback(process.terminate)
        ca_cert, ca_key = _openssl_ca(work)
        cluster_ca = Path(k3s.data_dir / "server/tls/server-ca.crt").read_text()
        system_roots = Path("/etc/ssl/certs/ca-certificates.crt").read_text()
        mcp = _mcp_inputs(work, fixture.mcp_fixtures.dex_host) if fixture.mcp_fixtures is not None else None
        fixture = rendered_fixture.fill(
            fixture,
            rendered_fixture.RuntimeInputs(
                ca_cert=ca_cert, ca_key=ca_key, system_roots=system_roots, cluster_ca=cluster_ca
            ),
            models,
            mcp,
        )
        k3s.import_images(layouts, required_images=_deployment_images(fixture.objects))
        for crd in crds:
            k3s.kubectl("apply", "-f", str(crd))
        # Namespaces first, then CRD-backed and the rest; one apply tolerates ordering within a kind only.
        namespaces = [d for d in fixture.objects if d["kind"] == "Namespace"]
        rest = [d for d in fixture.objects if d["kind"] != "Namespace"]
        k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(namespaces))
        print("apply:", k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(rest)))
        logger.info("applied %s objects", len(rest))

        failed: list[str] = []
        deployments = [d["metadata"]["name"] for d in rest if d["kind"] == "Deployment"]
        deadline = time.monotonic() + DEPLOY_TIMEOUT_SECONDS
        for name in deployments:
            remaining = max(5, int(deadline - time.monotonic()))
            out = k3s.kubectl(
                "wait", "--for=condition=Available", f"deployment/{name}", "-n", fixture.namespace,
                f"--timeout={remaining}s", check=False,
            )  # fmt: skip
            if "condition met" not in out:
                failed.append(name)
        if failed:
            print(k3s.report(fixture.namespace))
            raise RuntimeError(f"Deployments not Available: {failed}")
        _resolve_on_node(k3s, fixture)
        access = None
        if mcp is not None and fixture.mcp_fixtures is not None:
            ca_bundle = work / "ca-certificates.crt"
            ca_bundle.write_text(system_roots + mcp.dex_ca_cert)
            cleanup.enter_context(
                _port_forward(
                    k3s,
                    fixture.namespace,
                    fixture.mcp_fixtures.actions_service,
                    mcp.actions_origin,
                    fixture.mcp_fixtures.actions_port,
                )
            )
            access = McpFixtureAccess(
                ca_bundle=ca_bundle,
                operator_secret=fixture.mcp_fixtures.operator_secret,
                actions_mcp_url=f"{mcp.actions_origin}/mcp",
            )
        token = k3s.kubectl(
            "-n", fixture.namespace, "create", "token", TESTING_SERVICE_ACCOUNT, f"--audience={AUDIENCE}"
        ).strip()
        print("memory:", run("free", "-m"))
        yield Stack(k3s=k3s, namespace=fixture.namespace, base_url=fixture.app_origin, token=token, mcp_fixtures=access)
        print(k3s.report(fixture.namespace))
