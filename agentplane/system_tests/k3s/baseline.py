"""Owned k3s daemon and retained infrastructure compatibility.

Every helper that installs or preserves the retained node belongs in this compatibility boundary.
Per-action workloads and their cleanup live in stack.py and must certify cleanliness before reuse.
"""

import contextlib
import hashlib
import io
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.egress.kubernetes_audience import KUBERNETES_AUDIENCE
from agentplane.system_tests.driver.tracing import tracer
from agentplane.system_tests.k3s import bootstrap, iptables_rule, pki_infrastructure, release_manifests
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)
BOOTSTRAP_TIMEOUT_SECONDS = 180
STOP_TIMEOUT_SECONDS = 60
POD_CIDR = "10.42.0.0/16"
CONTAINERD_SOCKET = "/run/k3s/containerd/containerd.sock"
CONTAINERD_DEBUG_SOCKET = "/run/k3s/containerd/debug.sock"


def run(
    *command: str,
    check: bool = True,
    input_text: str | None = None,
    timeout: float | None = None,
    stdout_only: bool = False,
) -> str:
    result = subprocess.run(command, capture_output=True, text=True, input=input_text, check=False, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(
            f"{command[:3]} exited {result.returncode}\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )
    if stdout_only:
        if result.stderr:
            logger.warning("%s stderr: %s", command[:3], result.stderr.rstrip())
        return result.stdout
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
    _record_baseline_transition("reset_begin", data_dir)
    targets = [line.split()[1] for line in Path("/proc/mounts").read_text().splitlines()]
    for target in reversed(targets):
        if target.startswith(("/var/lib/kubelet/", "/run/k3s/", "/run/netns/")):
            run("umount", "--lazy", target)
    for entry in [*data_dir.iterdir(), *(data_dir / "agent").glob("*")]:
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
    _record_baseline_transition("reset_end", data_dir)


def _archive_json(archive: tarfile.TarFile, name: str, value: object) -> None:
    encoded = json.dumps(value).encode()
    entry = tarfile.TarInfo(name)
    entry.size = len(encoded)
    archive.addfile(entry, io.BytesIO(encoded))


def _prepare_docker_namespaces() -> None:
    """Let nested K3s manage the action's private network and cgroup namespaces."""
    if not Path("/.dockerenv").exists() or Path("/proc/self/cgroup").read_text().strip() != "0::/":
        raise RuntimeError("expected a Docker action with private namespaces")
    # kube-proxy and the CNI configure network sysctls. Keep the guest-wide /proc/sys
    # read-only; these sysctls belong to the action's network namespace.
    network_sysctls = "/proc/sys/net"
    if os.statvfs(network_sysctls).f_flag & os.ST_RDONLY:
        run("mount", "--bind", network_sysctls, network_sysctls)
        run("mount", "-o", "remount,rw,bind", network_sysctls)
    root = Path("/sys/fs/cgroup")
    if not os.statvfs(root).f_flag & os.ST_RDONLY:
        return
    run("mount", "-o", "remount,rw", str(root))
    # Like kind's entrypoint, move existing processes out before enabling child controllers.
    # https://github.com/kubernetes-sigs/kind/blob/main/images/base/files/usr/local/bin/entrypoint
    init = root / "init"
    init.mkdir(exist_ok=True)
    for pid in (root / "cgroup.procs").read_text().split():
        with contextlib.suppress(ProcessLookupError):
            (init / "cgroup.procs").write_text(pid)
    controllers = (root / "cgroup.controllers").read_text().split()
    (root / "cgroup.subtree_control").write_text(" ".join(f"+{controller}" for controller in controllers))


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
        self._database_cleanup = contextlib.ExitStack()
        # CRI publishes an imported image's config-ID alias before its pinned tag.
        self._fixture_image_ids: set[str] = set()

    def protect_image_layouts(self, images: dict[str, Path]) -> None:
        """Protect current fixture images even before CRI publishes their named references."""

        def add_descriptor(layout: Path, descriptor: dict[str, Any]) -> None:
            algorithm, digest = descriptor["digest"].split(":", 1)
            manifest = json.loads((layout / "blobs" / algorithm / digest).read_text())
            if "config" in manifest:
                self._fixture_image_ids.add(manifest["config"]["digest"])
            else:
                for child in manifest["manifests"]:
                    add_descriptor(layout, child)

        for layout in images.values():
            index = json.loads((layout / "index.json").read_text())
            for descriptor in index["manifests"]:
                add_descriptor(layout, descriptor)

    def kubectl(
        self,
        *args: str,
        check: bool = True,
        input_text: str | None = None,
        timeout: float | None = None,
        stdout_only: bool = False,
    ) -> str:
        return run(
            str(self.binary),
            "kubectl",
            "--kubeconfig",
            str(self.kubeconfig),
            *args,
            check=check,
            input_text=input_text,
            timeout=timeout,
            stdout_only=stdout_only,
        )

    @tracer.start_as_current_span("system_test.k3s.start")
    def start(
        self,
        *,
        layers_in_memory: bool,
        preserve_image_store: bool = True,
        database_in_memory: bool = False,
        detached: bool = False,
    ) -> subprocess.Popen[bytes]:
        if os.environ.get("AGENTPLANE_K3S_REQUIRE_KVM") == "1":
            _prepare_docker_namespaces()
        _reset_previous_stack(self.data_dir)
        _shadow_kernel_sysctls()
        if database_in_memory:
            # Control-plane commits must not share the bulk guest-disk write queue.
            database = self.data_dir / "server/db"
            database.mkdir(parents=True)
            run("mount", "-t", "tmpfs", "-o", "size=256m", "tmpfs", str(database))
            self._database_cleanup.callback(run, "umount", str(database))
        # Unpacking image layers is bound by writes to the executor's disk. The tmpfs default cap, half of
        # memory, makes a stack that outgrows it fail on a full filesystem rather than on the OOM killer.
        layers = self.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
        if not preserve_image_store or layers.is_mount() != layers_in_memory:
            # Discard metadata and layers together on a fresh start or a medium change;
            # containerd metadata must never name snapshots this stack cannot see.
            if layers.is_mount():
                run("umount", str(layers))
            image_store = self.data_dir / "agent/containerd"
            if image_store.exists():
                shutil.rmtree(image_store)
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
                # K3s defaults to its issuer and the `k3s` audience. Production also accepts the
                # projected Kubernetes workload audience; include it here so this fixture exercises
                # the same token the egress proxy substitutes for a Sandbox.
                "--kube-apiserver-arg",
                f"api-audiences=https://kubernetes.default.svc.cluster.local,k3s,{KUBERNETES_AUDIENCE}",
                "--kubelet-arg",
                "feature-gates=ImageVolume=true",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=detached,
        )
        readiness = bootstrap.Bootstrap(
            process=process,
            binary=self.binary,
            kubeconfig=self.kubeconfig,
            log_path=self.log_path,
            deadline=time.monotonic() + BOOTSTRAP_TIMEOUT_SECONDS,
        )
        try:
            readiness.wait_for(
                "get", "node/env", "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}", expected="True"
            )
            # host-gw routes Pod traffic but does not translate it: without this, Pods cannot reach the internet.
            # A recycled runner already has the rule.
            iptables = self.data_dir / "data/current/bin/aux/iptables"  # the copy k3s ships; the host has none
            masquerade = ("POSTROUTING", "-s", POD_CIDR, "!", "-d", POD_CIDR, "-j", "MASQUERADE")
            iptables_rule.ensure_rule(iptables, table="nat", chain=masquerade[0], rule=masquerade[1:])
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            self._preserve_containerd_log()
            self._database_cleanup.close()
            raise
        return process

    def _preserve_containerd_log(self) -> None:
        source = self.data_dir / "agent/containerd/containerd.log"
        if source.exists():
            shutil.copyfile(source, self.log_path.with_name("containerd.log"))

    def crictl(self, *args: str, check: bool = True) -> str:
        config = self.work / "crictl.yaml"
        config.write_text(f"runtime-endpoint: unix://{CONTAINERD_SOCKET}\ntimeout: {STOP_TIMEOUT_SECONDS}\n")
        return run(str(self.binary), "crictl", "--config", str(config), *args, check=check, stdout_only=True)

    @tracer.start_as_current_span("system_test.images.prune")
    def prune_unused_images(self) -> None:
        """Reclaim dangling CRI ID aliases, preserving named, pinned and in-use images."""

        def obsolete_images() -> set[str]:
            images = json.loads(self.crictl("images", "-o", "json"))["images"]
            containers = json.loads(self.crictl("ps", "-a", "-o", "json"))["containers"]
            in_use = {container["imageRef"] for container in containers}
            return {
                image["id"]
                for image in images
                if not image.get("repoTags")
                and not image.get("repoDigests")
                and not image.get("pinned")
                and image["id"] not in in_use
                and image["id"] not in self._fixture_image_ids
            }

        obsolete = sorted(obsolete_images())
        if obsolete:
            # Kubelet GC can remove an ID between listing it and crictl's status check.
            # Verify the postcondition instead of failing on an already-removed image.
            output = self.crictl("rmi", *obsolete, check=False)
            with (undeclared_outputs_dir() / "image-prune.log").open("a") as log:
                log.write(json.dumps({"images": obsolete, "output": output}) + "\n")
            # CRI processes imports asynchronously: an ID can gain a named/pinned reference
            # while its dangling alias is being removed. Recheck eligibility as well as identity.
            if surviving := obsolete_images().intersection(obsolete):
                raise RuntimeError(f"obsolete containerd images survived cleanup: {sorted(surviving)}\n{output}")

    def report_storage(self, stage: str) -> None:
        """Capture the filesystem and kubelet signals behind node-pressure evictions."""
        output = undeclared_outputs_dir() / "storage" / stage
        output.mkdir(parents=True, exist_ok=True)
        paths = ["/", "/workspace", "/var/lib/kubelet", str(self.data_dir / "agent/containerd")]
        paths.append(str(self.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"))
        for name, command in [
            ("filesystems.txt", ["df", "-PT", "-B1", *paths]),
            ("inodes.txt", ["df", "-Pi", *paths]),
            ("containerd-usage.txt", ["du", "-x", "-B1", "--max-depth=2", str(self.data_dir / "agent/containerd")]),
            ("images.txt", [str(self.binary), "ctr", "--address", CONTAINERD_SOCKET, "-n", "k8s.io", "images", "ls"]),
        ]:
            (output / name).write_text(run(*command, check=False, timeout=15))
        (output / "cri-images.json").write_text(self.crictl("images", "-o", "json", check=False))
        for name, resource in [
            ("summary.json", "/api/v1/nodes/env/proxy/stats/summary"),
            ("config.json", "/api/v1/nodes/env/proxy/configz"),
            ("metrics.txt", "/api/v1/nodes/env/proxy/metrics"),
        ]:
            (output / name).write_text(
                self.kubectl("get", "--raw", resource, "--request-timeout=5s", check=False, timeout=10)
            )
        (output / "resources.yaml").write_text(
            self.kubectl(
                "get", "nodes,pods,events", "-A", "-o", "yaml", "--request-timeout=5s", check=False, timeout=10
            )
        )

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
        self._database_cleanup.close()
        self.cleanup_stopped()

    def cleanup_stopped(self) -> None:
        """Remove Pods after an owned detached server has exited between test actions."""
        self._preserve_containerd_log()
        binaries = self.data_dir / "data/current/bin"
        with contextlib.ExitStack() as cleanup:
            teardown_log = self.log_path.with_name("containerd-teardown.log")
            log = cleanup.enter_context(teardown_log.open("wb"))
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
            try:
                for attempt in Retrying(
                    stop=stop_after_delay(STOP_TIMEOUT_SECONDS),
                    wait=wait_fixed(1),
                    retry=retry_if_exception_type(RuntimeError),
                    reraise=True,
                ):
                    if containerd.poll() is not None:
                        raise RuntimeError(f"standalone containerd exited with {containerd.returncode}")
                    with attempt:
                        self.crictl("pods")
                self.crictl("rmp", "--all", "--force")
                # Rebuilds reset the kubelet's image ages. Remove stale ID aliases while
                # the kubelet is stopped, before its two-minute GC age can permit evictions.
                self.prune_unused_images()
            except Exception as exc:
                log.flush()
                for source in (teardown_log, self.log_path.with_name("containerd.log")):
                    destination = undeclared_outputs_dir() / source.name
                    if source != destination and source.exists():
                        with contextlib.suppress(OSError):
                            shutil.copyfile(source, destination)
                tail = teardown_log.read_bytes()[-4096:].decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"containerd teardown failed (exit={containerd.poll()}): {exc}\n"
                    f"standalone containerd log tail:\n{tail or '<empty>'}"
                ) from exc

    def import_images(self, images: dict[str, Path]) -> None:
        """Import Bazel OCI layouts, keyed by the image reference each loads as, into
        containerd one image at a time. Blob digests are preserved, and a blob containerd
        already holds (an earlier image's, or on a recycled runner the last stack's) is not streamed again."""
        self.protect_image_layouts(images)
        self.prune_unused_images()
        layers = self.data_dir / "agent/containerd/io.containerd.snapshotter.v1.overlayfs"
        filesystem = os.statvfs(layers)
        # Sweep after imports consume half the starting headroom. Native filesystem
        # checks avoid per-image CRI round trips.
        prune_below_bytes = filesystem.f_bavail * filesystem.f_frsize // 2
        for image, layout in images.items():
            with tracer.start_as_current_span("system_test.image.import", attributes={"image": image}):
                index = json.loads((layout / "index.json").read_text())
                for descriptor in index["manifests"]:
                    descriptor.setdefault("annotations", {})["org.opencontainers.image.ref.name"] = image
                blobs = {blob.relative_to(layout).as_posix(): blob for blob in (layout / "blobs").glob("*/*")}
                stored = self._stored_blobs()
                missing = {name: blobs[name] for name in blobs.keys() - stored}
                self._import_layout(index["manifests"], missing)
            # Retargeting a fixture tag leaves its old CRI config-ID alias behind.
            if missing:
                filesystem = os.statvfs(layers)
                if filesystem.f_bavail * filesystem.f_frsize < prune_below_bytes:
                    self.prune_unused_images()
        # Retargeting to a fully cached version can also leave an old alias, without growing
        # the store during the group. Reclaim those aliases before yielding to the fixture.
        self.prune_unused_images()

    def _stored_blobs(self) -> set[str]:
        """The OCI layout paths (`blobs/sha256/<hex>`) of the blobs containerd holds."""
        digests = run(
            str(self.binary), "ctr", "--address", CONTAINERD_SOCKET, "-n", "k8s.io", "content", "ls", "--quiet"
        )
        return {"blobs/" + digest.replace(":", "/", 1) for digest in digests.split()}

    def _import_layout(self, manifests: list[dict[str, Any]], blobs: dict[str, Path]) -> None:
        with tempfile.TemporaryFile() as output:
            with subprocess.Popen(
                [
                    str(self.binary),
                    "ctr",
                    "--address",
                    CONTAINERD_SOCKET,
                    "-n",
                    "k8s.io",
                    "images",
                    "import",
                    # Sandboxes are created after stack setup, and the fixture's local tags have no
                    # registry fallback. Keep kubelet image GC from deleting an unused future image.
                    "--label=io.cri-containerd.pinned=pinned",
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


_BASELINE_ROOT = Path("/var/lib/k3s-persistence-v7")
_BASELINE_MARKER = _BASELINE_ROOT / "server.json"
_RETAINED_LOG_LIMIT = 8 * 1024 * 1024
# BuildBuddy reads this at the action workspace root to invalidate its snapshot family:
# https://github.com/buildbuddy-io/buildbuddy/blob/798749e61eb83e2523ba06a48ff195b62b375198/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L3550-L3557
_INVALIDATE_SNAPSHOT_MARKER = ".BUILDBUDDY_INVALIDATE_SNAPSHOT"
_BAZEL_TEST_SETUP_INPUT = Path("external/bazel_tools/tools/test/test-setup.sh")


def _record_baseline_transition(stage: str, data_dir: Path) -> None:
    """Keep the ownership and filesystem evidence needed to explain a later failed attach.

    The record lives outside the data directory that a cold start deletes. It is diagnostic only:
    the marker and the exact process identity still determine which server may be stopped.
    """
    if not _BASELINE_ROOT.exists():
        return
    marker = None
    if _BASELINE_MARKER.exists():
        try:
            marker = json.loads(_BASELINE_MARKER.read_text())
        except OSError, json.JSONDecodeError:
            marker = {"unreadable": True}
    identity = marker.get("identity") if isinstance(marker, dict) else None
    pid = identity.get("pid") if isinstance(identity, dict) else None
    event = {
        "time": time.time(),
        "stage": stage,
        "pid": os.getpid(),
        "target": os.environ.get("TEST_TARGET"),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "marker": marker,
        "marked_process_start_ticks": _baseline_process_start(int(pid)) if pid is not None else None,
        "containerd_config_exists": (data_dir / "agent/etc/containerd/config.toml").exists(),
        "cni_config_dir_exists": (data_dir / "agent/etc/cni/net.d").exists(),
        "server_tls_dir_exists": (data_dir / "server/tls").exists(),
    }
    with (_BASELINE_ROOT / "transitions.jsonl").open("a") as output:
        output.write(json.dumps(event, sort_keys=True) + "\n")


def capture_retained_logs(data_dir: Path, outputs: Path) -> None:
    """Retain bounded daemon logs even when attaching to or stopping the baseline fails."""
    sources = {
        "k3s-retained.log": _BASELINE_ROOT / "k3s.log",
        "containerd-retained.log": data_dir / "agent/containerd/containerd.log",
        "containerd-teardown-retained.log": _BASELINE_ROOT / "containerd-teardown.log",
        "baseline-transitions.jsonl": _BASELINE_ROOT / "transitions.jsonl",
    }
    for name, source in sources.items():
        try:
            with source.open("rb") as log, (outputs / name).open("wb") as destination:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - _RETAINED_LOG_LIMIT))
                destination.write(log.read(_RETAINED_LOG_LIMIT))
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("could not capture retained k3s log %s", source)


def _baseline_process_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except FileNotFoundError:
        return None
    return None if stat[0] == "Z" else stat[19]


def _baseline_identity(k3s: K3s, pid: int) -> dict[str, str | int]:
    ticks = _baseline_process_start(pid)
    if ticks is None:
        raise RuntimeError(f"k3s process {pid} exited")
    # A retained process can be alive with a wedged API. Bound the subprocess as well as kubectl's
    # HTTP request so the warm-cache check cannot wedge the entire test action.
    resources = json.loads(k3s.kubectl(
        "get", "node/env", "namespace/kube-system", "-o", "json", "--request-timeout=5s", timeout=10,
    ))["items"]  # fmt: skip
    node = next(resource for resource in resources if resource["kind"] == "Node")
    namespace = next(resource for resource in resources if resource["kind"] == "Namespace")
    internal_ip = next(address["address"] for address in node["status"]["addresses"] if address["type"] == "InternalIP")
    if internal_ip != k3s.node_ip:
        raise RuntimeError(f"retained node InternalIP {internal_ip} differs from current {k3s.node_ip}")
    return {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid": pid,
        "process_start_ticks": ticks,
        "namespace_uid": namespace["metadata"]["uid"],
        "node_uid": node["metadata"]["uid"],
        "node_internal_ip": internal_ip,
    }


def fingerprint(
    binary: Path,
    crds: Sequence[Path],
    baseline: list[dict[str, Any]],
    images: dict[str, Path],
    *,
    layers_in_memory: bool,
    helpers: dict[str, Path],
) -> str:
    digest = hashlib.sha256(b"k3s-persistence-v7\0")
    digest.update(b"tmpfs\0" if layers_in_memory else b"disk\0")
    for name, path in [
        ("k3s", binary),
        ("baseline.py", Path(__file__)),
        ("bootstrap.py", Path(bootstrap.__file__)),
        ("pki_infrastructure.py", Path(pki_infrastructure.__file__)),
        ("iptables_rule.py", Path(iptables_rule.__file__)),
        ("release_manifests.py", Path(release_manifests.__file__)),
        *sorted(helpers.items()),
        *((f"crds/{path.name}", path) for path in crds),
    ]:
        # Names and fixed-size digests frame the inputs, including helper changes that affect the
        # retained baseline. Stream the k3s executable instead of holding another copy in memory.
        digest.update(name.encode() + b"\0")
        with path.open("rb") as source:
            digest.update(hashlib.file_digest(source, "sha256").digest())
    digest.update(json.dumps(baseline, sort_keys=True).encode())
    for reference, layout in sorted(images.items()):
        digest.update(reference.encode())
        # OCI index manifests bind the config and layer digests without hashing every layer tarball.
        digest.update((layout / "index.json").read_bytes())
    return digest.hexdigest()


def write_marker(marker: dict[str, Any]) -> None:
    temporary = _BASELINE_MARKER.with_suffix(".tmp")
    temporary.write_text(json.dumps(marker))
    temporary.replace(_BASELINE_MARKER)


def stop_owned(k3s: K3s, previous: dict[str, Any]) -> None:
    _record_baseline_transition("stop_owned_begin", k3s.data_dir)
    identity = previous["identity"]
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if current_boot != identity["boot_id"]:
        return
    pid = int(identity["pid"])
    ticks = _baseline_process_start(pid)
    if ticks is not None and ticks != identity["process_start_ticks"]:
        raise RuntimeError("persistence marker PID was reused; refusing to reset an unowned process")
    if ticks is not None:
        os.kill(pid, signal.SIGTERM)
        for attempt in Retrying(
            stop=stop_after_delay(STOP_TIMEOUT_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(RuntimeError),
            reraise=True,
        ):
            with attempt:
                if _baseline_process_start(pid) is not None:
                    raise RuntimeError(f"owned k3s process {pid} is still alive")
    k3s.log_path = _BASELINE_ROOT / "k3s.log"
    k3s.cleanup_stopped()
    _record_baseline_transition("stop_owned_end", k3s.data_dir)


def stop_previous() -> None:
    """Leave retention mode by stopping only the server recorded in the ownership marker."""
    if _BASELINE_MARKER.exists():
        retained = K3s(_BASELINE_ROOT, _BASELINE_ROOT / "k3s")
        stop_owned(retained, json.loads(_BASELINE_MARKER.read_text()))
        _BASELINE_MARKER.unlink()


def _executor_action_workspace_root() -> Path | None:
    """Find the BuildBuddy action root using Bazel paths and a declared command input.

    Pytest changes into the runfiles tree, so its cwd and TEST_SRCDIR are not the workspace root.
    The test action's temp and output paths share the root; the declared test-setup input validates
    that root against the action layout before writing a provider marker there.
    """
    if os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") != "1":
        return None
    test_tmpdir_raw = os.environ.get("TEST_TMPDIR")
    outputs_dir_raw = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if not test_tmpdir_raw or not outputs_dir_raw:
        return None
    try:
        test_tmpdir = Path(test_tmpdir_raw).resolve(strict=True)
        outputs_dir = Path(outputs_dir_raw).resolve(strict=True)
        root = Path(os.path.commonpath((test_tmpdir, outputs_dir)))
        test_tmpdir_relative = test_tmpdir.relative_to(root)
        outputs_dir_relative = outputs_dir.relative_to(root)
    except OSError, ValueError:
        return None
    if (
        not test_tmpdir_relative.parts
        or test_tmpdir_relative.parts[0] != "_tmp"
        or not outputs_dir_relative.parts
        or outputs_dir_relative.parts[0] != "bazel-out"
        or not (root / _BAZEL_TEST_SETUP_INPUT).is_file()
    ):
        return None
    return root


def _write_executor_snapshot_invalidation_marker() -> None:
    workspace_root = _executor_action_workspace_root()
    if workspace_root is None:
        raise RuntimeError("cannot identify authorized BuildBuddy action workspace for snapshot invalidation")
    (workspace_root / _INVALIDATE_SNAPSHOT_MARKER).touch()


@contextlib.contextmanager
def invalidate_snapshot_on_action_error(*, enabled: bool) -> Iterator[None]:
    """Discard this executor's snapshot when a persistent stack lifecycle fails."""
    try:
        yield
    except BaseException as original_error:
        if enabled and os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") == "1":
            try:
                _write_executor_snapshot_invalidation_marker()
            except Exception as marker_error:
                logger.exception("could not invalidate BuildBuddy snapshot after persistent stack failure")
                original_error.add_note(f"BuildBuddy snapshot invalidation marker failed: {marker_error!r}")
        raise


def open_baseline(source: Path, fingerprint: str, *, layers_in_memory: bool) -> tuple[K3s, dict[str, Any], bool]:
    # Protect diagnostic callers too: only BuildBuddy's remote exec property grants this lifecycle.
    if os.environ.get("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE") != "1":
        raise RuntimeError("retaining k3s requires BuildBuddy's executor persistence permission")
    _BASELINE_ROOT.mkdir(exist_ok=True)
    binary = _BASELINE_ROOT / "k3s"
    k3s = K3s(_BASELINE_ROOT, binary)
    previous = json.loads(_BASELINE_MARKER.read_text()) if _BASELINE_MARKER.exists() else None
    if previous is not None:
        if previous.get("state") not in {"clean", "dirty"}:
            raise RuntimeError("invalid persistence state marker")
        if previous["fingerprint"] == fingerprint and previous["state"] == "clean":
            try:
                current = _baseline_identity(k3s, int(previous["identity"]["pid"]))
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                logger.warning("retained k3s is unhealthy; rebuilding the owned baseline: %s", exc)
            else:
                if current == previous["identity"]:
                    previous["state"] = "dirty"
                    write_marker(previous)
                    return k3s, previous, True
                logger.warning("retained k3s identity changed; rebuilding the owned baseline")
        else:
            logger.info(
                "rebuilding k3s baseline: state=%s, inputs_match=%s",
                previous["state"],
                previous["fingerprint"] == fingerprint,
            )
        stop_owned(k3s, previous)
        _BASELINE_MARKER.unlink()
    shutil.copy2(source, binary)
    previous_outputs = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    os.environ["TEST_UNDECLARED_OUTPUTS_DIR"] = str(_BASELINE_ROOT)
    try:
        process = k3s.start(layers_in_memory=layers_in_memory, detached=True)
    finally:
        if previous_outputs is None:
            os.environ.pop("TEST_UNDECLARED_OUTPUTS_DIR", None)
        else:
            os.environ["TEST_UNDECLARED_OUTPUTS_DIR"] = previous_outputs
    # Until the marker is durable, this action still owns the process directly. A failed identity
    # probe or marker write must not leave an unmarked detached server for the next action to reset.
    with contextlib.ExitStack() as starting:
        starting.callback(k3s.stop, process)
        marker = {"state": "dirty", "fingerprint": fingerprint, "identity": _baseline_identity(k3s, process.pid)}
        write_marker(marker)
        starting.pop_all()
    return k3s, marker, False
