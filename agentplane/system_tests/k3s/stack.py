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
import contextvars
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.cluster import AGENT_SERVICE_ACCOUNT, AUDIENCE, Cluster
from agentplane.system_tests.k3s import (
    block_storage,
    certificates,
    guest_logs,
    process_traces,
    rendered_fixture,
    vm_sandboxes,
)
from agentplane.system_tests.k3s.bootstrap import Bootstrap
from agentplane.system_tests.k3s.iptables_rule import ensure_rule
from agentplane.system_tests.tracing import tracer
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
CONTAINERD_DEBUG_SOCKET = "/run/k3s/containerd/debug.sock"
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


@tracer.start_as_current_span("system_test.k3s.reset")
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

    @tracer.start_as_current_span("system_test.k3s.start")
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
        # The image store survives runner recycling; a previous stack's daemon log need not.
        (self.data_dir / "agent/containerd/containerd.log").unlink(missing_ok=True)
        # K3s's v3 template imports this directory. The root-owned Unix socket stays inside
        # this isolated test action and exposes Go's standard CPU/runtime profilers.
        diagnostics = self.data_dir / "agent/etc/containerd/config-v3.toml.d/diagnostics.toml"
        diagnostics.parent.mkdir(parents=True, exist_ok=True)
        diagnostics.write_text(f'[debug]\n  address = "{CONTAINERD_DEBUG_SOCKET}"\n')
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
            self._preserve_containerd_log()
            raise
        return process

    def _preserve_containerd_log(self) -> None:
        source = self.data_dir / "agent/containerd/containerd.log"
        if source.exists():
            shutil.copyfile(source, self.log_path.with_name("containerd.log"))

    @contextlib.contextmanager
    def profile_containerd(self, kind: str, seconds: int, destination: Path) -> Iterator[None]:
        """Capture a bounded, real Go profiler sample without delaying stack readiness."""
        if not Path(CONTAINERD_DEBUG_SOCKET).is_socket():
            raise RuntimeError("containerd debug socket is unavailable")
        with destination.open("wb") as output, destination.with_suffix(destination.suffix + ".log").open("wb") as log:
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "ctr",
                    "pprof",
                    "--debug-socket",
                    CONTAINERD_DEBUG_SOCKET,
                    kind,
                    f"--seconds={seconds}s",
                ],
                stdout=output,
                stderr=log,
            )
            try:
                yield
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            else:
                try:
                    code = process.wait(timeout=seconds + 10)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait()
                    raise RuntimeError(f"containerd {kind} profiler timed out; see {log.name}") from exc
                if code or destination.stat().st_size == 0:
                    raise RuntimeError(f"containerd {kind} profiler failed ({code}); see {log.name}")

    @tracer.start_as_current_span("system_test.k3s.stop")
    def stop(self, process: subprocess.Popen[bytes]) -> None:
        """End k3s, then remove every Pod, so the next stack on a recycled runner finds only images.

        k3s's containerd dies with it (a parent-death signal) while the Pods' shims outlive it, so a containerd
        started here removes the Pods, with no kubelet to recreate them or remove them concurrently. Pausing
        k3s instead would block containerd. Preserve its separate log before the cleanup process starts."""
        process.terminate()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
        self._preserve_containerd_log()
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

    def import_images(self, images: dict[str, Path]) -> None:
        """Import Bazel OCI layouts (`.rloc` files, keyed by the image reference each loads as) into
        containerd one image at a time. Blob digests are preserved, and a blob containerd
        already holds (an earlier image's, or on a recycled runner the last stack's) is not streamed again."""
        for image, rloc in images.items():
            with tracer.start_as_current_span("system_test.image.import", attributes={"image": image}):
                layout = get_required_path(rloc.read_text().strip())
                index = json.loads((layout / "index.json").read_text())
                for descriptor in index["manifests"]:
                    descriptor.setdefault("annotations", {})["org.opencontainers.image.ref.name"] = image
                blobs = {blob.relative_to(layout).as_posix(): blob for blob in (layout / "blobs").glob("*/*")}
                stored = self._stored_blobs()
                self._import_layout(index["manifests"], {name: blobs[name] for name in blobs.keys() - stored})

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


@tracer.start_as_current_span("system_test.pki.fixture_ca")
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


@tracer.start_as_current_span("system_test.network.resolve")
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
    inputs: StackInputs, *, models: rendered_fixture.Models, validate_vm_shutdown: Callable[[], bool]
) -> Iterator[Stack]:
    """The stack with every Deployment Available and LLM ingress forwarding to `models`."""
    crds = sorted(get_required_path("_main/agentplane/crds/manifests").glob("crd-*.yaml"))
    fixture = inputs.fixture
    fixture_images = {_ci_image(rloc): rloc for rloc in inputs.image_layouts}
    guest_images = {_ci_image(rloc): rloc for rloc in inputs.guest_image_layouts}
    if fixture.vm_sandboxes != bool(guest_images):
        raise ValueError("VM deployment and guest image inputs do not match")

    work = Path(tempfile.mkdtemp())
    k3s = K3s(work, inputs.binary)
    with (
        contextlib.ExitStack() as trace_cleanup,
        contextlib.ExitStack() as block_cleanup,
        contextlib.ExitStack() as cleanup,
    ):
        with tracer.start_as_current_span("system_test.stack.setup") as setup:
            traces = trace_cleanup.enter_context(
                process_traces.ProcessTraces(k3s.node_ip, undeclared_outputs_dir() / "process-spans.jsonl")
            )
            # A VM stack's layers would leave too little memory for its guests (README § Image layers in memory).
            process = k3s.start(layers_in_memory=not fixture.vm_sandboxes)
            cleanup.callback(k3s.stop, process)
            setup.add_event("node Ready")
            cluster = Cluster(
                kubeconfig=k3s.kubeconfig, namespace=fixture.namespace, kubectl_command=(str(k3s.binary), "kubectl")
            )
            raw_block = block_storage.BlockStorage(work)

            def remove_work() -> None:
                if raw_block.closed:
                    shutil.rmtree(work, ignore_errors=True)
                else:
                    logger.error("preserving %s because a fixture loop device did not detach", work)

            block_cleanup.callback(remove_work)
            block_cleanup.enter_context(raw_block)
            with tracer.start_as_current_span("system_test.collector.expose"):
                k3s.kubectl(
                    "apply",
                    "-f",
                    "-",
                    input_text=yaml.safe_dump_all(process_traces.collector_objects(k3s.node_ip, traces)),
                )
            setup.add_event("trace collector ready")
            ca_cert, ca_key = _openssl_ca(work)
            cluster_ca = Path(k3s.data_dir / "server/tls/server-ca.crt").read_text()
            system_roots = Path("/etc/ssl/certs/ca-certificates.crt").read_text()
            mcp = _mcp_inputs(work, fixture.mcp_fixtures.dex_host) if fixture.mcp_fixtures is not None else None
            with tracer.start_as_current_span("system_test.fixture.configure"):
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
                k3s.import_images(infrastructure_images)
            cert_manager, trust_manager = certificates.releases()
            cleanup.callback(certificates.report, k3s.kubectl, cert_manager + trust_manager)
            # Import the next group's images during apply/readiness work for the preceding group. A single
            # worker preserves blob deduplication and joins before k3s teardown, including on setup failure.
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-import") as importing:
                pki_import = importing.submit(
                    contextvars.copy_context().run, k3s.import_images, certificates.images(cert_manager + trust_manager)
                )
                if fixture.vm_sandboxes:
                    vm_sandboxes.install(k3s.kubectl, releases)
                    setup.add_event("KubeVirt and Kyverno applied")
                # Applying a workload before its image is imported would pull it from the registry.
                pki_import.result()
                fixture_import = importing.submit(contextvars.copy_context().run, k3s.import_images, fixture_images)
                certificates.install(k3s.kubectl, cert_manager, trust_manager)
                setup.add_event("cert-manager and trust-manager ready")
                fixture_import.result()
                setup.add_event("fixture images imported" if guest_images else "images imported")
                if guest_images:
                    # The VM root image is this stack's largest cold import. Sampling proceeds while
                    # the importer and the rest of setup work; joining happens at fixture teardown.
                    outputs = undeclared_outputs_dir()
                    cleanup.enter_context(
                        k3s.profile_containerd("profile", 15, outputs / "containerd-import.cpu.pprof")
                    )
                    cleanup.enter_context(
                        k3s.profile_containerd("trace", 5, outputs / "containerd-import.runtime.trace")
                    )
                guest_import = importing.submit(contextvars.copy_context().run, k3s.import_images, guest_images)
                with tracer.start_as_current_span("system_test.crds.apply"):
                    for crd in crds:
                        k3s.kubectl("apply", "-f", str(crd))
                if fixture.vm_sandboxes:
                    vm_sandboxes.wait_for_apis(k3s.kubectl)
                setup.add_event("CRDs applied")
                # Namespaces first, then CRD-backed and the rest; one apply tolerates ordering within a kind only.
                namespaces = [d for d in fixture.objects if d["kind"] == "Namespace"]
                rest = [d for d in fixture.objects if d["kind"] != "Namespace"]
                with tracer.start_as_current_span("system_test.manifests.apply"):
                    k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(namespaces))
                    if fixture.vm_sandboxes:
                        cleanup.enter_context(raw_block.provisioning(cluster, node_name="env"))
                        setup.add_event("workspace provisioner ready")
                    print("apply:", k3s.kubectl("apply", "-f", "-", input_text=yaml.safe_dump_all(rest)))
                    logger.info("applied %s objects", len(rest))
                    setup.add_event("manifests applied")
                certificates.wait_for_issuance(k3s.kubectl, fixture.namespace, "cluster-internal-ca-bundle")
                setup.add_event("runner certificates issued and trust distributed")
                # Guest images serve Sandboxes created after stack readiness, so they can import during apply.
                guest_import.result()
                if guest_images:
                    setup.add_event("images imported")

            failed: list[str] = []
            deployments = [d["metadata"]["name"] for d in rest if d["kind"] == "Deployment"]
            deadline = time.monotonic() + DEPLOY_TIMEOUT_SECONDS
            for name in deployments:
                remaining = max(5, int(deadline - time.monotonic()))
                with tracer.start_as_current_span("system_test.deployment.ready", attributes={"deployment.name": name}):
                    out = k3s.kubectl(
                        "wait", "--for=condition=Available", f"deployment/{name}", "-n", fixture.namespace,
                        f"--timeout={remaining}s", check=False,
                    )  # fmt: skip
                    if "condition met" not in out:
                        failed.append(name)
            if failed:
                print(k3s.report(fixture.namespace))
                raise RuntimeError(f"Deployments not Available: {failed}")
            setup.add_event("Deployments Available")
            if fixture.vm_sandboxes:
                vm_sandboxes.wait_until_ready(k3s.kubectl)
                setup.add_event("KubeVirt and Kyverno ready")
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
            setup.add_event("stack ready")
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
        with tracer.start_as_current_span("system_test.stack.diagnostics"):
            print(k3s.report(fixture.namespace))
            if fixture.vm_sandboxes:
                (undeclared_outputs_dir() / "vm-sandboxes.txt").write_text(
                    vm_sandboxes.report(k3s.kubectl, fixture.namespace)
                )
