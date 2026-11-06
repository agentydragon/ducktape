"""VM-only per-action volumes, guest configuration and evidence collection."""

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.k3s.baseline import K3s
from agentplane.system_tests.vm import (
    block_storage,
    guest_logs,
    infrastructure as vm_infrastructure,
    sandboxes as vm_sandboxes,
)
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


class VmRuntime:
    def __init__(self, work: Path) -> None:
        self.releases = vm_infrastructure.releases()
        self._storage = block_storage.BlockStorage(work)

    def __enter__(self) -> VmRuntime:
        self._storage.__enter__()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._storage.__exit__(exc_type, exc, traceback)

    @property
    def closed(self) -> bool:
        return self._storage.closed

    @property
    def released_devices(self) -> int:
        return len(self._storage.devices)

    def baseline_objects(self) -> list[dict[str, Any]]:
        return vm_infrastructure.baseline_objects()

    def baseline_helpers(self) -> dict[str, Path]:
        # Retained KubeVirt/Kyverno mutations belong to vm_infrastructure.py. Guest and
        # workspace resources must disappear before stack.py can certify the baseline clean.
        return {"vm_infrastructure.py": Path(vm_infrastructure.__file__)}

    def baseline_identity(self, k3s: K3s) -> dict[str, str]:
        return vm_infrastructure.baseline_identity(k3s.kubectl)

    def storage_class_uid(self, k3s: K3s) -> str:
        return k3s.kubectl("get", "storageclass", block_storage.STORAGE_CLASS, "-o", "jsonpath={.metadata.uid}")

    def configure(self, objects: list[dict[str, Any]]) -> None:
        vm_sandboxes.select_boot_evidence(objects, enabled=os.environ.get("AGENTPLANE_VM_BOOT_EVIDENCE") == "1")
        vm_sandboxes.select_native_profile(
            objects,
            enabled=os.environ.get("AGENTPLANE_VM_NATIVE_PROFILE") == "1",
            profile_root=os.environ.get("K3S_PROFILE_GUEST_ROOT") == "1",
        )

    def install(self, k3s: K3s) -> None:
        vm_infrastructure.install(k3s.kubectl, self.releases)

    def wait_for_apis(self, k3s: K3s) -> None:
        vm_infrastructure.wait_for_apis(k3s.kubectl)

    def wait_until_ready(self, k3s: K3s) -> None:
        vm_infrastructure.wait_until_ready(k3s.kubectl)

    def provisioning(self, cluster: Cluster) -> AbstractContextManager[None]:
        return self._storage.provisioning(cluster, node_name="env")

    @contextmanager
    def capture(self, k3s: K3s, namespace: str, validate_shutdown: Callable[[], bool]) -> Iterator[None]:
        with (
            guest_logs.capture(
                log_root=Path("/var/log/pods"),
                pod_root=Path("/var/lib/kubelet/pods"),
                storage_root=k3s.data_dir / "storage",
                namespace=namespace,
                kubevirt_namespace=vm_infrastructure.KUBEVIRT_NAMESPACE,
                destination=undeclared_outputs_dir() / "vm-artifacts",
                validate_shutdown=validate_shutdown,
                require_boot_evidence=os.environ.get("AGENTPLANE_VM_BOOT_EVIDENCE") == "1",
            ),
            _host_qemu_capture(k3s, namespace),
        ):
            yield

    def report(self, k3s: K3s, namespace: str) -> str:
        return vm_sandboxes.report(k3s.kubectl, namespace)

    def clean(self, k3s: K3s) -> None:
        if json.loads(k3s.kubectl("get", "vm,vmi", "-A", "-o", "json"))["items"]:
            raise RuntimeError("fixture-owned virtual machines survived namespace deletion")
        # The provisioner owns this cluster-scoped object outside the rendered fixture.
        k3s.kubectl("delete", "storageclass", block_storage.STORAGE_CLASS, "--wait=true")
        if k3s.kubectl("get", "storageclass", block_storage.STORAGE_CLASS, "--ignore-not-found=true", "-o", "name"):
            raise RuntimeError("fixture-owned StorageClass survived cleanup")


def _qemu_processes(output: TextIO) -> None:
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if not comm.read_text().startswith("qemu"):
                continue
            process = comm.parent
            output.write(f"HOST_QEMU {process.name}\n")
            output.writelines(
                f"{field}: {process.joinpath(field).read_text().replace(chr(0), ' ')}\n"
                for field in ("cmdline", "cgroup", "status")
            )
            for task in process.joinpath("task").glob("*"):
                output.write(f"HOST_QEMU_THREAD {task.name}\n")
                for field in ("comm", "stat", "wchan", "syscall", "stack"):
                    try:
                        output.write(f"{field}: {task.joinpath(field).read_text()}\n")
                    except OSError as error:
                        output.write(f"{field}: {error!r}\n")
        except FileNotFoundError:
            continue
    output.flush()


def _qemu_snapshot(k3s: K3s, namespace: str, pod: str, output: TextIO, stop: threading.Event) -> None:
    _qemu_processes(output)
    for uri in ("qemu+unix:///session?socket=/var/run/libvirt/virtqemud-sock", "qemu:///system"):
        base = ("-n", namespace, "exec", pod, "-c", "compute", "--", "virsh", "-c", uri)
        try:
            names = k3s.kubectl(*base, "list", "--name", timeout=2, stdout_only=True).split()
            output.write(f"VIRSH {uri} DOMAINS {names!r}\n")
        except Exception as error:
            output.write(f"VIRSH {uri} ERROR {error!r}\n")
            output.flush()
            continue
        if len(names) != 1:
            return
        for command, cpu_index in (
            ("query-status", None),
            ("query-cpus-fast", None),
            ("info registers -a", None),
            ("info lapic", 0),
            ("info lapic", 1),
            ("info pic", None),
            ("info irq", None),
            ("info clocks", None),
        ):
            if stop.is_set():
                return
            query: dict[str, Any] = {"execute": command}
            if command.startswith("info "):
                arguments: dict[str, Any] = {"command-line": command}
                if cpu_index is not None:
                    arguments["cpu-index"] = cpu_index
                query = {"execute": "human-monitor-command", "arguments": arguments}
            output.write(f"QEMU_PROBE {command} cpu={cpu_index}\n")
            output.flush()
            try:
                output.write(k3s.kubectl(*base, "qemu-monitor-command", names[0], json.dumps(query), timeout=2))
            except Exception as error:
                output.write(f"PROBE_ERROR {error!r}\n")
            output.write("\n")
            output.flush()
        return


def _observe_host_qemu(k3s: K3s, namespace: str, stop: threading.Event) -> None:
    artifacts = undeclared_outputs_dir() / "vm-artifacts"
    started: dict[Path, float] = {}
    last: dict[Path, float] = {}
    boot: set[Path] = set()
    samples: dict[Path, int] = {}
    while not stop.is_set():
        for console in (artifacts / "serial").glob("*/*/*.log"):
            lines = console.read_bytes().splitlines()
            if guest_logs.SHUTDOWN_COMPLETE in lines:
                continue
            requested = b"AGENTPLANE_GUEST_SHUTDOWN_REQUESTED" in lines
            if requested:
                now = time.monotonic()
                start = started.setdefault(console, now)
                if now - start < 5 or now - last.get(console, 0) < 4:
                    continue
                phase = "shutdown"
                last[console] = now
            elif console in boot:
                continue
            else:
                phase = "boot"
                boot.add(console)
            launcher = console.parents[1].name
            pod = launcher.removeprefix(f"{namespace}_").rsplit("_", 1)[0]
            directory = artifacts / "host-qemu" / launcher / console.parent.name
            directory.mkdir(parents=True, exist_ok=True)
            sample = samples.get(console, 0)
            samples[console] = sample + 1
            with (directory / f"{phase}-{sample}.txt").open("w") as output:
                output.write(f"HOST_QEMU_SNAPSHOT phase={phase} time={time.time()}\n")
                try:
                    _qemu_snapshot(k3s, namespace, pod, output, stop)
                except Exception as error:
                    output.write(f"SNAPSHOT_ERROR {error!r}\n")
        stop.wait(0.25)


@contextmanager
def _host_qemu_capture(k3s: K3s, namespace: str) -> Iterator[None]:
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="qemu-inspection") as pool:
        watching = pool.submit(_observe_host_qemu, k3s, namespace, stop)
        try:
            yield
        finally:
            stop.set()
            watching.result()
