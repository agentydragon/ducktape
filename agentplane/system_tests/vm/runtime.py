"""VM-only per-action volumes, guest configuration and evidence collection."""

import json
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.k3s.baseline import K3s
from agentplane.system_tests.vm import (
    block_storage,
    guest_logs,
    host_qemu,
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
        vm_sandboxes.select_libvirt_monitor_debug(
            objects, enabled=os.environ.get("AGENTPLANE_VM_LIBVIRT_MONITOR_DEBUG") == "1"
        )
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
        artifacts = undeclared_outputs_dir() / "vm-artifacts"
        passive_virtiofsd_timeline = host_qemu.virtiofsd_timeline_enabled()
        with (
            guest_logs.capture(
                log_root=Path("/var/log/pods"),
                pod_root=Path("/var/lib/kubelet/pods"),
                storage_root=k3s.data_dir / "storage",
                namespace=namespace,
                kubevirt_namespace=vm_infrastructure.KUBEVIRT_NAMESPACE,
                destination=artifacts,
                validate_shutdown=validate_shutdown,
                require_boot_evidence=os.environ.get("AGENTPLANE_VM_BOOT_EVIDENCE") == "1",
                record_shutdown_diagnostic=os.environ.get("AGENTPLANE_VM_SHUTDOWN_DIAGNOSTIC") == "1",
            ),
            host_qemu.capture(k3s.kubectl, namespace, artifacts, passive_virtiofsd_timeline=passive_virtiofsd_timeline),
        ):
            yield

    def report(self, k3s: K3s, namespace: str) -> str:
        if not host_qemu.virtiofsd_timeline_enabled():
            host_qemu.snapshot_pending(k3s.kubectl, namespace, undeclared_outputs_dir() / "vm-artifacts")
        return vm_sandboxes.report(k3s.kubectl, namespace)

    def clean(self, k3s: K3s) -> None:
        if json.loads(k3s.kubectl("get", "vm,vmi", "-A", "-o", "json"))["items"]:
            raise RuntimeError("fixture-owned virtual machines survived namespace deletion")
        # The provisioner owns this cluster-scoped object outside the rendered fixture.
        k3s.kubectl("delete", "storageclass", block_storage.STORAGE_CLASS, "--wait=true")
        if k3s.kubectl("get", "storageclass", block_storage.STORAGE_CLASS, "--ignore-not-found=true", "-o", "name"):
            raise RuntimeError("fixture-owned StorageClass survived cleanup")
