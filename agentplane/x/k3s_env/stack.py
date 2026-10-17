"""A bare-k3s copy of the `agentplane-testing` stack on one RBE action, for tests to drive.

Starts k3s (no kube-proxy, no Cilium), loads the Bazel-built service images, applies the composed
manifest and yields the app's address and a token once every Deployment is Available. It fails at the
first Deployment that does not, with the Pod states and logs of whatever did not start.
"""

import contextlib
import io
import json
import logging
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentplane.acceptance.conftest import AUDIENCE, TESTING_SERVICE_ACCOUNT
from agentplane.x.k3s_env import fixture_chart
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 480
# k3s's default Pod CIDR.
POD_CIDR = "10.42.0.0/16"


def run(*command: str, check: bool = True, input_text: str | None = None) -> str:
    result = subprocess.run(command, capture_output=True, text=True, input=input_text, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{command[:3]} exited {result.returncode}: {result.stderr[-2000:]}{result.stdout[-1000:]}")
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


class K3s:
    def __init__(self, work: Path, binary: Path) -> None:
        self.work = work
        self.binary = binary
        self.kubeconfig = work / "kubeconfig"
        self.data_dir = Path("/var/lib/k3s-env")
        # The address Pods use to reach host-side processes: the apiserver and the cluster DNS.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            self.node_ip: str = probe.getsockname()[0]

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
                "--disable-kube-proxy",
                "--flannel-backend=host-gw",
                "--disable-network-policy",
                "--node-ip",
                self.node_ip,
                "--kubelet-arg",
                f"cluster-dns={self.node_ip}",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.kubeconfig.exists() and "env" in self.kubectl("get", "nodes", "-o", "name", check=False):
                break
            time.sleep(1)
        ready = self.kubectl("wait", "--for=condition=Ready", "node/env", "--timeout=180s", check=False)
        if "condition met" not in ready:
            raise RuntimeError(f"k3s not ready (exit={process.poll()}): {ready}\n{self.log_path.read_text()[-6000:]}")
        self._serve_dns_and_api_on_node()
        # host-gw routes Pod traffic but does not translate it: without this, Pods cannot reach the internet.
        iptables = self.data_dir / "data/current/bin/aux/iptables"  # the copy k3s ships; the host has none
        run(str(iptables), "-t", "nat", "-A", "POSTROUTING", "-s", POD_CIDR, "!", "-d", POD_CIDR, "-j", "MASQUERADE")
        return process

    def _serve_dns_and_api_on_node(self) -> None:
        """Without kube-proxy the `kube-dns` ClusterIP is dead: CoreDNS listens on the node IP instead."""
        for name in ("coredns", "local-path-provisioner"):
            while "not found" in self.kubectl("-n", "kube-system", "get", f"deploy/{name}", check=False):
                time.sleep(1)
            self.kubectl(
                "-n", "kube-system", "set", "env", f"deploy/{name}",
                f"KUBERNETES_SERVICE_HOST={self.node_ip}", "KUBERNETES_SERVICE_PORT=6443",
            )  # fmt: skip
        corefile = self.kubectl(
            "-n", "kube-system", "get", "configmap/coredns", "-o", "jsonpath={.data.Corefile}"
        ).replace(".:53 {", f".:53 {{\n    bind {self.node_ip}", 1)
        self.kubectl(
            "-n", "kube-system", "patch", "configmap/coredns", "--type", "merge", "-p",
            json.dumps({"data": {"Corefile": corefile}}),
        )  # fmt: skip
        self.kubectl(
            "-n", "kube-system", "patch", "deploy/coredns", "-p",
            json.dumps({"spec": {"template": {"spec": {"hostNetwork": True, "dnsPolicy": "Default"}}}}),
        )  # fmt: skip

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

    def report(self) -> str:
        parts = [
            self.kubectl("get", "pods", "-A", "-o", "wide", check=False),
            self.kubectl("get", "events", "-A", "--sort-by=.lastTimestamp", check=False)[-6000:],
        ]
        pods = self.kubectl("get", "pods", "-n", fixture_chart.NAMESPACE, "-o", "name", check=False).split()
        for pod in pods:
            ready = self.kubectl(
                "get",
                pod,
                "-n",
                fixture_chart.NAMESPACE,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                check=False,
            )
            if ready != "True":
                logs = self.kubectl(
                    "logs", pod, "-n", fixture_chart.NAMESPACE, "--all-containers", "--tail=40", check=False
                )
                previous = self.kubectl(
                    "logs",
                    pod,
                    "-n",
                    fixture_chart.NAMESPACE,
                    "--all-containers",
                    "--previous",
                    "--tail=40",
                    check=False,
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
class Stack:
    k3s: K3s
    base_url: str
    token: str


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
def running_stack(*, litellm_key: str | None) -> Iterator[Stack]:
    """The stack with every Deployment Available. `litellm_key` points LLM traffic at the public LiteLLM;
    None leaves it at the cluster-internal Service, which does not exist here, so no turn can run."""
    crds = sorted(get_required_path("_main/agentplane/crds/manifests").glob("crd-*.yaml"))
    layouts = sorted(get_required_path("_main/agentplane/x/k3s_env").glob("*_layout.rloc"))

    work = Path(tempfile.mkdtemp())
    k3s = K3s(work, get_required_path(os.environ["K3S_BINARY_RLOCATION"]))
    process = k3s.start()
    try:
        ca_cert, ca_key = _openssl_ca(work)
        cluster_ca = Path(k3s.data_dir / "server/tls/server-ca.crt").read_text()
        everything = fixture_chart.objects(
            node_ip=k3s.node_ip,
            ca_cert=ca_cert,
            ca_key=ca_key,
            system_roots=Path("/etc/ssl/certs/ca-certificates.crt").read_text(),
            cluster_ca=cluster_ca,
            litellm_key=litellm_key,
        )
        k3s.import_images(layouts, required_images=_deployment_images(everything))
        for crd in crds:
            k3s.kubectl("apply", "-f", str(crd))
        # Namespaces first, then CRD-backed and the rest; one apply tolerates ordering within a kind only.
        namespaces = [d for d in everything if d["kind"] == "Namespace"]
        rest = [d for d in everything if d["kind"] != "Namespace"]
        k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(namespaces))
        print("apply:", k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(rest), check=False)[-3000:])
        logger.info("applied %s objects", len(rest))

        failed: list[str] = []
        deployments = [d["metadata"]["name"] for d in rest if d["kind"] == "Deployment"]
        deadline = time.monotonic() + DEPLOY_TIMEOUT_SECONDS
        for name in deployments:
            remaining = max(5, int(deadline - time.monotonic()))
            out = k3s.kubectl(
                "wait", "--for=condition=Available", f"deployment/{name}", "-n", fixture_chart.NAMESPACE,
                f"--timeout={remaining}s", check=False,
            )  # fmt: skip
            if "condition met" not in out:
                failed.append(name)
        if failed:
            print(k3s.report())
            raise RuntimeError(f"Deployments not Available: {failed}")
        app_ip = k3s.kubectl(
            "get", "pod", "-n", fixture_chart.NAMESPACE, "-l", "app.kubernetes.io/name=app",
            "-o", "jsonpath={.items[0].status.podIP}",
        )  # fmt: skip
        token = k3s.kubectl(
            "-n", fixture_chart.NAMESPACE, "create", "token", TESTING_SERVICE_ACCOUNT, f"--audience={AUDIENCE}"
        ).strip()
        print("memory:", run("free", "-m"))
        yield Stack(k3s=k3s, base_url=f"http://{app_ip}:8080", token=token)
        print(k3s.report())
    finally:
        process.terminate()
        shutil.rmtree(work, ignore_errors=True)
