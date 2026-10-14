"""A bare-k3s copy of the `agentplane-testing` stack on one RBE action, for tests to drive.

Starts k3s (no kube-proxy, no Cilium), loads the Bazel-built service images, applies the rewritten
manifest and yields the app's address and a token once every Deployment is Available. It fails at the
first Deployment that does not, with the Pod states and logs of whatever did not start.
"""

import contextlib
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

import yaml

from agentplane.acceptance.conftest import AUDIENCE, TESTING_SERVICE_ACCOUNT
from agentplane.x.k3s_env import render
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

K3S_VERSION = "v1.33.4+k3s1"
CNPG_MANIFEST_URL = "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.0/cnpg-1.30.0.yaml"
# Electric exits until history-service's migration has created its publication; its restart back-off reaches 5 min.
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


def _read_member(archive: tarfile.TarFile, name: str) -> bytes:
    member = archive.extractfile(name)
    assert member
    return member.read()


def _layer_sizes(tarball: Path) -> dict[str, int]:
    """Layer path (a content digest in docker-format tarballs) to size, per `manifest.json`."""
    with tarfile.open(tarball) as archive:
        manifest = json.loads(_read_member(archive, "manifest.json"))
        return {layer: archive.getmember(layer).size for image in manifest for layer in image["Layers"]}


class K3s:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.binary = work / "k3s"
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
        base = f"https://github.com/k3s-io/k3s/releases/download/{K3S_VERSION.replace('+', '%2B')}"
        run("curl", "-fsSL", "-o", str(self.binary), f"{base}/k3s")
        run("chmod", "+x", str(self.binary))
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
        ).replace(
            ".:53 {",
            f".:53 {{\n    bind {self.node_ip}\n"
            f"    rewrite name exact postgres-rw.{render.NAMESPACE}.svc.cluster.local "
            f"{render.PRIMARY_SERVICE}.{render.NAMESPACE}.svc.cluster.local",
            1,
        )
        self.kubectl(
            "-n", "kube-system", "patch", "configmap/coredns", "--type", "merge", "-p",
            json.dumps({"data": {"Corefile": corefile}}),
        )  # fmt: skip
        self.kubectl(
            "-n", "kube-system", "patch", "deploy/coredns", "-p",
            json.dumps({"spec": {"template": {"spec": {"hostNetwork": True, "dnsPolicy": "Default"}}}}),
        )  # fmt: skip

    def import_images(self, tarballs: list[Path]) -> None:
        layers: dict[str, int] = {}
        for tarball in tarballs:
            layers.update(_layer_sizes(tarball))
        print(
            f"{len(layers)} distinct layers, {sum(layers.values()) >> 20} MiB compressed across {len(tarballs)} images"
        )
        for tarball in tarballs:
            print(f"importing {tarball.parent.name} ({tarball.stat().st_size >> 20} MiB)")
            print(run("df", "-h", "/var/lib", "/tmp", check=False))
            run(
                str(self.binary),
                "ctr",
                "--address",
                "/run/k3s/containerd/containerd.sock",
                "-n",
                "k8s.io",
                "images",
                "import",
                str(tarball),
            )

    def install_cnpg_operator(self) -> None:
        """The release manifest of the operator the cluster runs (`cluster/cdk8s/cnpg_operator.py`)."""
        manifest = self.work / "cnpg.yaml"
        run("curl", "-fsSL", "-o", str(manifest), CNPG_MANIFEST_URL)
        self.kubectl("apply", "--server-side", "-f", str(manifest))
        # No kube-proxy: the operator dials the apiserver directly, and the apiserver cannot reach the
        # webhook Service, so an unreachable webhook must not fail the request. The operator still needs
        # the configurations to exist: it injects its CA into them at startup.
        for kind, name in (
            ("mutatingwebhookconfiguration", "cnpg-mutating-webhook-configuration"),
            ("validatingwebhookconfiguration", "cnpg-validating-webhook-configuration"),
        ):
            configuration = json.loads(self.kubectl("get", f"{kind}/{name}", "-o", "json"))
            for webhook in configuration["webhooks"]:
                webhook["failurePolicy"] = "Ignore"
                webhook["timeoutSeconds"] = 2
            self.kubectl("apply", "--server-side", "--force-conflicts", "-f", "-", input_text=json.dumps(configuration))
        # The release manifest limits the operator to 100m CPU; throttled, it misses leader-election renewals and
        # restarts, and the Cluster waits for it.
        self.kubectl(
            "-n", "cnpg-system", "patch", "deploy/cnpg-controller-manager", "--type=json", "-p",
            '[{"op": "remove", "path": "/spec/template/spec/containers/0/resources/limits"}]',
        )  # fmt: skip
        self.kubectl(
            "-n", "cnpg-system", "set", "env", "deploy/cnpg-controller-manager",
            f"KUBERNETES_SERVICE_HOST={self.node_ip}", "KUBERNETES_SERVICE_PORT=6443",
        )  # fmt: skip
        waited = self.kubectl(
            "-n", "cnpg-system", "wait", "--for=condition=Available", "deploy/cnpg-controller-manager", "--timeout=240s",
            check=False,
        )  # fmt: skip
        if "condition met" not in waited:
            raise RuntimeError(
                f"CNPG operator not Available: {waited}\n"
                + self.kubectl("-n", "cnpg-system", "get", "pods", "-o", "wide", check=False)
                + self.kubectl("-n", "cnpg-system", "describe", "pods", check=False)[-3000:]
                + self.kubectl("-n", "cnpg-system", "logs", "deploy/cnpg-controller-manager", "--tail=40", check=False)
            )

    def report(self) -> str:
        parts = [
            self.kubectl("get", "pods", "-A", "-o", "wide", check=False),
            self.kubectl("get", "events", "-A", "--sort-by=.lastTimestamp", check=False)[-6000:],
        ]
        parts.append(self.kubectl("get", "cluster,database", "-A", check=False))
        parts.append(
            self.kubectl("-n", "cnpg-system", "logs", "deploy/cnpg-controller-manager", "--tail=25", check=False)
        )
        parts.append(
            self.kubectl(
                "-n", "cnpg-system", "logs", "deploy/cnpg-controller-manager", "--previous", "--tail=25", check=False
            )
        )
        pods = self.kubectl("get", "pods", "-n", render.NAMESPACE, "-o", "name", check=False).split()
        for pod in pods:
            ready = self.kubectl(
                "get",
                pod,
                "-n",
                render.NAMESPACE,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                check=False,
            )
            if ready != "True":
                logs = self.kubectl("logs", pod, "-n", render.NAMESPACE, "--all-containers", "--tail=40", check=False)
                previous = self.kubectl(
                    "logs", pod, "-n", render.NAMESPACE, "--all-containers", "--previous", "--tail=40", check=False
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


@contextlib.contextmanager
def running_stack(*, litellm_key: str | None) -> Iterator[Stack]:
    """The stack with every Deployment Available. `litellm_key` points LLM traffic at the public LiteLLM;
    None leaves it at the cluster-internal Service, which does not exist here, so no turn can run."""
    manifest = get_required_path("_main/cluster/generated/agentplane-testing/agentplane-testing.k8s.yaml")
    crds = sorted(get_required_path("_main/agentplane/crds/manifests").glob("crd-*.yaml"))
    tarballs = sorted(get_required_path("_main/agentplane/x/k3s_env").glob("*_load/tarball.tar"))

    work = Path(tempfile.mkdtemp())
    k3s = K3s(work)
    process = k3s.start()
    try:
        ca_cert, ca_key = _openssl_ca(work)
        k3s.import_images(tarballs)
        for crd in crds:
            k3s.kubectl("apply", "-f", str(crd))
        k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(render.kubevirt_stub_crds()))
        k3s.install_cnpg_operator()
        rewritten = render.rewrite(
            yaml.safe_load_all(manifest.read_text()),
            overrides={} if litellm_key is None else {"litellm-key-cheap-experiments": litellm_key},
            node_ip=k3s.node_ip,
            external_litellm=litellm_key is not None,
        )
        cluster_ca = Path(k3s.data_dir / "server/tls/server-ca.crt").read_text()
        extra = render.egress_ca_objects(
            ca_cert=ca_cert,
            ca_key=ca_key,
            system_roots=Path("/etc/ssl/certs/ca-certificates.crt").read_text(),
            cluster_ca=cluster_ca,
        )
        everything = [*rewritten, *render.extra_namespaces(), *extra]
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
                "wait", "--for=condition=Available", f"deployment/{name}", "-n", render.NAMESPACE,
                f"--timeout={remaining}s", check=False,
            )  # fmt: skip
            if "condition met" not in out:
                failed.append(name)
        if failed:
            print(k3s.report())
            raise RuntimeError(f"Deployments not Available: {failed}")
        app_ip = k3s.kubectl(
            "get", "pod", "-n", render.NAMESPACE, "-l", "app.kubernetes.io/name=agentplane-app",
            "-o", "jsonpath={.items[0].status.podIP}",
        )  # fmt: skip
        token = k3s.kubectl(
            "-n", render.NAMESPACE, "create", "token", TESTING_SERVICE_ACCOUNT, f"--audience={AUDIENCE}"
        ).strip()
        print("memory:", run("free", "-m"))
        yield Stack(k3s=k3s, base_url=f"http://{app_ip}:8080", token=token)
        print(k3s.report())
    finally:
        process.terminate()
        shutil.rmtree(work, ignore_errors=True)
