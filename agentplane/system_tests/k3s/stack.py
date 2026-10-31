"""A bare-k3s Agentplane stack on one RBE action, for tests to drive.

Starts k3s with kube-proxy and no Cilium, loads the Bazel-built service images, applies the composed
manifest and yields the app's address and a token once every Deployment is Available. It fails at the
first Deployment that does not, with the Pod states and logs of whatever did not start.

The test process reaches the app (and Dex) by their in-cluster Service names, which it resolves
through `/etc/hosts`: OIDC redirects name them. Dex's
certificate comes from a CA made per stack, which `McpFixtureAccess.ca_bundle` adds to the system roots.
In MCP fixtures the app and standalone MCP edge use loopback origins, served by port-forwards, so
their plain HTTP URLs satisfy the OAuth endpoint contract.
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
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.cluster import AGENT_SERVICE_ACCOUNT, AUDIENCE, Cluster
from agentplane.system_tests.k3s import block_storage, certificates, guest_logs, rendered_fixture, vm_sandboxes
from agentplane.system_tests.k3s.bootstrap import Bootstrap
from agentplane.system_tests.k3s.iptables_rule import ensure_rule
from agentplane.testing import tls
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

DEPLOY_TIMEOUT_SECONDS = 480
BOOTSTRAP_TIMEOUT_SECONDS = 180
STOP_TIMEOUT_SECONDS = 60
# k3s's default Pod CIDR.
POD_CIDR = "10.42.0.0/16"
CONTAINERD_SOCKET = "/run/k3s/containerd/containerd.sock"
# Marks the `/etc/hosts` lines a stack wrote, which the next stack on a recycled runner replaces.
HOSTS_MARKER = "# k3s-env"


def run(*command: str, check: bool = True, input_text: str | None = None) -> str:
    result = subprocess.run(command, capture_output=True, text=True, input=input_text, check=False)
    if check and result.returncode:
        raise RuntimeError(
            f"{command[:3]} exited {result.returncode}\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )
    return result.stdout + result.stderr


def _shadow_kernel_sysctls() -> None:
    """The Firecracker guest has no CONFIG_KEYS; the kubelet refuses to start without these files. The
    shadow outlives the stack, so the next stack on a recycled runner finds it mounted."""
    if Path("/proc/sys/kernel/keys/root_maxkeys").exists():
        return
    fake = Path("/run/k3s-env-proc-sys-kernel")
    fake.mkdir(exist_ok=True)
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


def _reset_previous_stack(data_dir: Path) -> None:
    """The RBE platform recycles runners, so the VM may still hold the last test's stack, whose Pods its
    teardown removed. Keep containerd's store, whose images are addressed by digest, and k3s's unpacked
    binaries; reset the rest."""
    if not data_dir.exists():
        return
    targets = [line.split()[1] for line in Path("/proc/mounts").read_text().splitlines()]
    for target in reversed(targets):
        if target.startswith(("/var/lib/kubelet/", "/run/k3s/", "/run/netns/")):
            run("umount", "--lazy", target)
    for entry in [*data_dir.iterdir(), *(data_dir / "agent").iterdir()]:
        if entry.name not in {"agent", "data", "containerd"}:
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
    for state in (
        "/var/lib/kubelet",
        "/var/lib/cni",
        "/var/log/pods",
        "/var/log/containers",
        "/run/k3s",
        "/run/flannel",
    ):
        shutil.rmtree(state, ignore_errors=True)
    # The kubelet spends about 3 s on each container cgroup it finds without a container before it
    # registers the node, which the last stack's 40-odd added up to over two minutes.
    for root in [*Path("/sys/fs/cgroup").glob("kubepods*"), *Path("/sys/fs/cgroup").glob("*/kubepods*")]:
        for directory, _, _ in root.walk(top_down=False):
            directory.rmdir()


def _archive_json(archive: tarfile.TarFile, name: str, value: object) -> None:
    encoded = json.dumps(value).encode()
    entry = tarfile.TarInfo(name)
    entry.size = len(encoded)
    archive.addfile(entry, io.BytesIO(encoded))


@dataclass
class Timeline:
    """When each step of bringing the stack up finished, in seconds after the timeline began, and how
    long each image took to import. Each entry is also appended to `k3s-timeline.jsonl` in the test's
    undeclared outputs as it happens, so a run that hangs shows how far it got."""

    started: float = field(default_factory=time.monotonic)
    steps: list[tuple[str, float]] = field(default_factory=list)
    imports: list[tuple[str, float]] = field(default_factory=list)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def mark(self, step: str) -> None:
        at = round(time.monotonic() - self.started, 1)
        self.steps.append((step, at))
        self._record({"step": step, "at": at})

    def image_imported(self, image: str, seconds: float) -> None:
        # `git.allegedly.works/ducktape-ci/agentplane-app:unset` or `quay.io/kubevirt/virt-api@sha256:…`
        name = image.rsplit("/", 1)[-1].partition("@")[0].partition(":")[0]
        self.imports.append((name, round(seconds, 1)))
        self._record({"image": name, "seconds": round(seconds, 1), "at": round(time.monotonic() - self.started, 1)})

    def summary(self) -> str:
        steps = ", ".join(f"{step} {at}" for step, at in self.steps)
        imports = ", ".join(f"{name} {seconds}" for name, seconds in sorted(self.imports, key=lambda i: -i[1]))
        return f"seconds after start: {steps}\nimage imports, seconds, slowest first: {imports}"

    def _record(self, entry: dict[str, str | float]) -> None:
        with self._write_lock, (undeclared_outputs_dir() / "k3s-timeline.jsonl").open("a") as out:
            out.write(json.dumps(entry) + "\n")


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

    def start(self, *, layers_in_memory: bool) -> subprocess.Popen[bytes]:
        _reset_previous_stack(self.data_dir)
        _shadow_kernel_sysctls()
        # Unpacking image layers is bound by writes to the executor's disk. The tmpfs default cap, half of
        # memory, makes a stack that outgrows it fail on a full filesystem rather than on the OOM killer.
        layers = self.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
        if layers.is_mount() != layers_in_memory:
            # A recycled runner's last stack kept its layers on the other medium, so containerd's metadata
            # names snapshots this one would not see: start from an empty store.
            if layers.is_mount():
                run("umount", str(layers))
            shutil.rmtree(self.data_dir / "agent/containerd", ignore_errors=True)
            if layers_in_memory:
                layers.mkdir(parents=True)
                run("mount", "-t", "tmpfs", "tmpfs", str(layers))
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
            # A recycled runner already has the rule.
            iptables = self.data_dir / "data/current/bin/aux/iptables"  # the copy k3s ships; the host has none
            masquerade = ("POSTROUTING", "-s", POD_CIDR, "!", "-d", POD_CIDR, "-j", "MASQUERADE")
            ensure_rule(iptables, table="nat", chain=masquerade[0], rule=masquerade[1:])
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        return process

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        """End k3s, then remove every Pod, so the next stack on a recycled runner finds only images.

        k3s's containerd dies with it (a parent-death signal) while the Pods' shims outlive it, so a containerd
        started here removes the Pods, with no kubelet to recreate them or remove them concurrently. Pausing
        k3s instead would block containerd, whose output k3s copies to its log."""
        process.terminate()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
        binaries = self.data_dir / "data/current/bin"
        crictl_config = self.work / "crictl.yaml"
        crictl_config.write_text(f"runtime-endpoint: unix://{CONTAINERD_SOCKET}\ntimeout: {STOP_TIMEOUT_SECONDS}\n")
        crictl = (str(self.binary), "crictl", "--config", str(crictl_config))
        with contextlib.ExitStack() as cleanup:
            log = cleanup.enter_context(self.log_path.with_name("containerd-teardown.log").open("wb"))
            containerd = cleanup.enter_context(
                subprocess.Popen(
                    [str(binaries / "containerd"), "-c", str(self.data_dir / "agent/etc/containerd/config.toml")],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    # The PATH k3s gives its containerd, which finds the runc shim there.
                    env=os.environ | {"PATH": f"{binaries}:{os.environ['PATH']}:{binaries / 'aux'}"},
                )
            )
            cleanup.callback(containerd.terminate)
            for attempt in Retrying(
                stop=stop_after_delay(STOP_TIMEOUT_SECONDS),
                wait=wait_fixed(1),
                retry=retry_if_exception_type(RuntimeError),
                reraise=True,
            ):
                with attempt:
                    run(*crictl, "pods")
            run(*crictl, "rmp", "--all", "--force")

    def import_images(self, images: dict[str, Path], timeline: Timeline) -> None:
        """Import Bazel OCI layouts (`.rloc` files, keyed by the image reference each loads as) into
        containerd one image at a time, timing each. Blob digests are preserved, and a blob containerd
        already holds (an earlier image's, or on a recycled runner the last stack's) is not streamed again."""
        for image, rloc in images.items():
            started = time.monotonic()
            layout = get_required_path(rloc.read_text().strip())
            index = json.loads((layout / "index.json").read_text())
            for descriptor in index["manifests"]:
                descriptor.setdefault("annotations", {})["org.opencontainers.image.ref.name"] = image
            blobs = {blob.relative_to(layout).as_posix(): blob for blob in (layout / "blobs").glob("*/*")}
            stored = self._stored_blobs()
            self._import_layout(index["manifests"], {name: blobs[name] for name in blobs.keys() - stored})
            timeline.image_imported(image, time.monotonic() - started)

    def _stored_blobs(self) -> set[str]:
        """The OCI layout paths (`blobs/sha256/<hex>`) of the blobs containerd holds."""
        digests = run(
            str(self.binary), "ctr", "--address", CONTAINERD_SOCKET, "-n", "k8s.io", "content", "ls", "--quiet"
        )
        return {"blobs/" + digest.replace(":", "/", 1) for digest in digests.split()}

    def _import_layout(self, manifests: list[dict[str, Any]], blobs: dict[str, Path]) -> None:
        with tempfile.TemporaryFile() as output:
            with subprocess.Popen(
                [str(self.binary), "ctr", "--address", CONTAINERD_SOCKET, "-n", "k8s.io", "images", "import", "-"],
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
    # The standalone edge's MCP endpoint, at the origin its OAuth server names itself by.
    mcp_edge_url: str


@dataclass(frozen=True)
class StackInputs:
    fixture: rendered_fixture.RenderedFixture
    binary: Path
    image_layouts: tuple[Path, ...]
    guest_image_layouts: tuple[Path, ...]


@dataclass(frozen=True)
class Stack:
    k3s: K3s
    cluster: Cluster
    base_url: str
    token: str
    mcp_fixtures: McpFixtureAccess | None


def _mcp_inputs(work: Path, dex_host: str) -> rendered_fixture.McpInputs:
    ca = tls.make_ca("test-k3s-fixture-ca")
    cert, key = tls.issue_leaf(ca, dex_host, work)
    # Free ports now; port-forwards listen once the app and MCP edge are Available.
    with socket.socket() as probe, socket.socket() as app_probe:
        probe.bind(("127.0.0.1", 0))
        app_probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        app_port = app_probe.getsockname()[1]
    return rendered_fixture.McpInputs(
        dex_ca_cert=ca.cert_pem.decode(),
        dex_cert=cert.read_text(),
        dex_key=key.read_text(),
        mcp_edge_origin=f"http://127.0.0.1:{port}",
        app_origin=f"http://127.0.0.1:{app_port}",
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
    """Resolve the Services a browser reaches by name for this process, as the cluster DNS does for Pods,
    replacing what the last stack on a recycled runner wrote."""
    hosts = Path("/etc/hosts")
    kept = [line for line in hosts.read_text().splitlines() if not line.endswith(HOSTS_MARKER)]
    for service in fixture.browser_services:
        address = k3s.kubectl(
            "get", "service", "-n", fixture.namespace, service.name, "-o", "jsonpath={.spec.clusterIP}"
        )
        kept.append(f"{address} {service.host} {service.fqdn} {HOSTS_MARKER}")
    # Rewritten in place: the file may be a mount.
    hosts.write_text("\n".join(kept) + "\n")


def _ci_image(rloc: Path) -> str:
    """The reference fixture workloads use for a Bazel-built image, named by its layout's `.rloc`."""
    name = rloc.stem.removesuffix("_layout")
    repository = "runner" if name == "runner" else "agentplane-" + name.replace("_", "-")
    return f"git.allegedly.works/ducktape-ci/{repository}:unset"


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
def running_stack(
    inputs: StackInputs,
    *,
    models: rendered_fixture.Models,
    timeline: Timeline,
    validate_vm_shutdown: Callable[[], bool],
) -> Iterator[Stack]:
    """The stack with every Deployment Available, LLM ingress forwarding to `models`, each step of
    bringing it up marked on `timeline`.

    All deployment and image inputs are resolved once by the suite's session fixture.
    """
    crds = sorted(get_required_path("_main/agentplane/crds/manifests").glob("crd-*.yaml"))
    fixture = inputs.fixture
    fixture_images = {_ci_image(rloc): rloc for rloc in inputs.image_layouts}
    guest_images = {_ci_image(rloc): rloc for rloc in inputs.guest_image_layouts}
    if fixture.vm_sandboxes != bool(guest_images):
        raise ValueError("VM deployment and guest image inputs do not match")

    work = Path(tempfile.mkdtemp())
    k3s = K3s(work, inputs.binary)
    # A VM stack's layers would leave too little memory for its guests (README § Image layers in memory).
    process = k3s.start(layers_in_memory=not fixture.vm_sandboxes)
    timeline.mark("node Ready")
    cluster = Cluster(
        kubeconfig=k3s.kubeconfig, namespace=fixture.namespace, kubectl_command=(str(k3s.binary), "kubectl")
    )
    with contextlib.ExitStack() as cleanup:
        raw_block = block_storage.BlockStorage(work)

        def remove_work() -> None:
            if raw_block.closed:
                shutil.rmtree(work, ignore_errors=True)
            else:
                logger.error("preserving %s because a fixture loop device did not detach", work)

        cleanup.callback(remove_work)
        cleanup.enter_context(raw_block)
        cleanup.callback(k3s.stop, process)
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
        deployment_images = _deployment_images(fixture.objects)
        if used := deployment_images & guest_images.keys():
            raise ValueError(f"Guest images are used by Deployments: {sorted(used)}")
        if missing := deployment_images - fixture_images.keys():
            raise ValueError(f"Fixture workloads lack OCI layouts for: {sorted(missing)}")
        if fixture.vm_sandboxes:
            releases = vm_sandboxes.releases()
            infrastructure_images = vm_sandboxes.images(releases)
            k3s.import_images(infrastructure_images, timeline)
        cert_manager, trust_manager = certificates.releases()
        cleanup.callback(certificates.report, k3s.kubectl, cert_manager + trust_manager)
        # Import the next group's images during apply/readiness work for the preceding group. A single
        # worker preserves blob deduplication and joins before k3s teardown, including on setup failure.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-import") as importing:
            pki_import = importing.submit(
                k3s.import_images, certificates.images(cert_manager + trust_manager), timeline
            )
            if fixture.vm_sandboxes:
                vm_sandboxes.install(k3s.kubectl, releases)
                timeline.mark("KubeVirt and Kyverno applied")
            # Applying a workload before its image is imported would pull it from the registry.
            pki_import.result()
            fixture_import = importing.submit(k3s.import_images, fixture_images, timeline)
            certificates.install(k3s.kubectl, cert_manager, trust_manager)
            timeline.mark("cert-manager and trust-manager ready")
            fixture_import.result()
            timeline.mark("fixture images imported" if guest_images else "images imported")
            guest_import = importing.submit(k3s.import_images, guest_images, timeline)
            for crd in crds:
                k3s.kubectl("apply", "-f", str(crd))
            if fixture.vm_sandboxes:
                vm_sandboxes.wait_for_apis(k3s.kubectl)
            timeline.mark("CRDs applied")
            # Namespaces first, then CRD-backed and the rest; one apply tolerates ordering within a kind only.
            namespaces = [d for d in fixture.objects if d["kind"] == "Namespace"]
            rest = [d for d in fixture.objects if d["kind"] != "Namespace"]
            k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(namespaces))
            if fixture.vm_sandboxes:
                cleanup.enter_context(raw_block.provisioning(cluster, node_name="env"))
                timeline.mark("workspace provisioner ready")
            print("apply:", k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(rest)))
            logger.info("applied %s objects", len(rest))
            timeline.mark("manifests applied")
            certificates.wait_for_issuance(k3s.kubectl, fixture.namespace, "cluster-internal-ca-bundle")
            timeline.mark("runner certificates issued and trust distributed")
            # Guest images serve Sandboxes created after stack readiness, so they can import during apply.
            guest_import.result()
            if guest_images:
                timeline.mark("images imported")

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
        timeline.mark("Deployments Available")
        if fixture.vm_sandboxes:
            vm_sandboxes.wait_until_ready(k3s.kubectl)
            timeline.mark("KubeVirt and Kyverno ready")
        _resolve_on_node(k3s, fixture)
        access = None
        if mcp is not None and fixture.mcp_fixtures is not None:
            ca_bundle = work / "ca-certificates.crt"
            ca_bundle.write_text(system_roots + mcp.dex_ca_cert)
            cleanup.enter_context(
                _port_forward(
                    k3s,
                    fixture.namespace,
                    fixture.mcp_fixtures.mcp_edge_service,
                    mcp.mcp_edge_origin,
                    fixture.mcp_fixtures.mcp_edge_port,
                )
            )
            cleanup.enter_context(
                _port_forward(
                    k3s,
                    fixture.namespace,
                    fixture.mcp_fixtures.app_service,
                    mcp.app_origin,
                    fixture.mcp_fixtures.app_port,
                )
            )
            access = McpFixtureAccess(
                ca_bundle=ca_bundle,
                operator_secret=fixture.mcp_fixtures.operator_secret,
                mcp_edge_url=f"{mcp.mcp_edge_origin}/mcp",
            )
        token = k3s.kubectl(
            "-n", fixture.namespace, "create", "token", AGENT_SERVICE_ACCOUNT, f"--audience={AUDIENCE}"
        ).strip()
        print("memory:", run("free", "-m"))
        timeline.mark("stack ready")
        if fixture.vm_sandboxes:
            cleanup.enter_context(
                guest_logs.capture(
                    log_root=Path("/var/log/pods"),
                    namespace=fixture.namespace,
                    kubevirt_namespace=vm_sandboxes.KUBEVIRT_NAMESPACE,
                    destination=undeclared_outputs_dir() / "vm-artifacts",
                    validate_shutdown=validate_vm_shutdown,
                )
            )
        yield Stack(k3s=k3s, cluster=cluster, base_url=fixture.app_origin, token=token, mcp_fixtures=access)
        print(k3s.report(fixture.namespace))
        if fixture.vm_sandboxes:
            (undeclared_outputs_dir() / "vm-sandboxes.txt").write_text(
                vm_sandboxes.report(k3s.kubectl, fixture.namespace)
            )
