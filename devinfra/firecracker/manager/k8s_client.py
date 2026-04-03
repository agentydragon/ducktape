"""Kubernetes client for managing Firecracker VM pods.

Each VM gets two PVCs from the LVM thin pool:
  1. Rootfs (volumeMode: Block) — thin snapshot of base, Firecracker drive
  2. Work (volumeMode: Filesystem) — thin-provisioned, holds snapshot files

The manager drives VM configuration (boot, snapshot/restore) through
the Firecracker API proxy on port 2026 after the pod is running.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from kubernetes import client, config

from devinfra.firecracker.manager.models import VMInfo, VMStatus

logger = logging.getLogger(__name__)

_NAMESPACE = "claude-sandbox"
_VM_LABEL = "app.kubernetes.io/managed-by"
_VM_LABEL_VALUE = "firecracker-manager"
_VM_ID_LABEL = "firecracker-vm-id"
_SNAPSHOT_LABEL = "firecracker-snapshot"

# Pod-internal paths. The manager references these when calling the
# Firecracker API to configure drives and snapshot paths.
ROOTFS_DEVICE_PATH = "/dev/rootfs"
WORK_MOUNT = "/work"
SNAPSHOT_MOUNT = "/snapshots"
KERNEL_PATH = "/opt/firecracker/vmlinux"
INITRAMFS_PATH = "/opt/firecracker/initramfs.cpio"

# StorageClass names — both from LVM thin pool on wyrm2 (VG: openebs-lvmvg).
# lvm-proxmox exists in devel (Filesystem). lvm-proxmox-block needs to be
# created (same VG, no fstype, volumeMode: Block support).
_BLOCK_SC = "lvm-proxmox-block"
_FS_SC = "lvm-proxmox"


class K8sVMClient:
    """Manages Firecracker VM pods and PVCs via the Kubernetes API."""

    def __init__(self) -> None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._core = client.CoreV1Api()

    # ── VM lifecycle ─────────────────────────────────────────────────────

    def create_vm(
        self, *, vm_image: str, base_rootfs_pvc: str, cpus: int, mem_mib: int, node_selector: dict[str, str] | None
    ) -> VMInfo:
        """Create a VM's PVCs + pod and return its info."""
        vm_id = secrets.token_hex(4)
        rootfs_pvc = self._create_pvc(
            _rootfs_pvc_name(vm_id),
            volume_mode="Block",
            storage_class=_BLOCK_SC,
            size="10Gi",
            labels={_VM_ID_LABEL: vm_id},
            data_source=base_rootfs_pvc,
        )
        work_pvc = self._create_pvc(
            _work_pvc_name(vm_id),
            volume_mode="Filesystem",
            storage_class=_FS_SC,
            size="10Gi",
            labels={_VM_ID_LABEL: vm_id},
        )
        spec = self._pod_spec(
            vm_id,
            vm_image=vm_image,
            rootfs_pvc=rootfs_pvc,
            work_pvc=work_pvc,
            cpus=cpus,
            mem_mib=mem_mib,
            node_selector=node_selector,
        )

        logger.info("Creating VM pod %s (cpus=%d, mem=%dMiB)", _pod_name(vm_id), cpus, mem_mib)
        self._core.create_namespaced_pod(namespace=_NAMESPACE, body=spec)
        return VMInfo(id=vm_id, pod_name=_pod_name(vm_id), status=VMStatus.CREATING)

    def delete_vm(self, vm_id: str) -> None:
        """Delete a VM pod and all its PVCs (rootfs, work, snapshot clone)."""
        logger.info("Deleting VM %s", vm_id)
        self._delete_if_exists("pod", _pod_name(vm_id))
        self._delete_if_exists("pvc", _rootfs_pvc_name(vm_id))
        self._delete_if_exists("pvc", _work_pvc_name(vm_id))
        self._delete_if_exists("pvc", f"firecracker-snap-{vm_id}")

    def list_vms(self) -> list[VMInfo]:
        pods = self._core.list_namespaced_pod(namespace=_NAMESPACE, label_selector=f"{_VM_LABEL}={_VM_LABEL_VALUE}")
        return [_vm_info_from_pod(pod) for pod in pods.items]

    def get_vm(self, vm_id: str) -> VMInfo | None:
        try:
            pod = self._core.read_namespaced_pod(name=_pod_name(vm_id), namespace=_NAMESPACE)
        except client.ApiException as e:
            if e.status == 404:
                return None
            raise
        return _vm_info_from_pod(pod)

    # ── Snapshot lifecycle ───────────────────────────────────────────────

    def create_snapshot_pvc(self, snapshot_name: str, size: str = "8Gi") -> str:
        """Create a filesystem PVC for snapshot memory + vmstate files."""
        return self._create_pvc(
            _snapshot_pvc_name(snapshot_name),
            volume_mode="Filesystem",
            storage_class=_FS_SC,
            size=size,
            labels={_SNAPSHOT_LABEL: snapshot_name},
        )

    def label_snapshot(self, vm_id: str, snapshot_name: str) -> None:
        """Record the source VM's rootfs PVC on the work PVC for restore."""
        self._core.patch_namespaced_persistent_volume_claim(
            name=_work_pvc_name(vm_id),
            namespace=_NAMESPACE,
            body={
                "metadata": {
                    "labels": {_SNAPSHOT_LABEL: snapshot_name, "firecracker-source-rootfs": _rootfs_pvc_name(vm_id)}
                }
            },
        )

    def get_snapshot_rootfs_pvc(self, snapshot_name: str) -> str:
        """Look up which rootfs PVC a snapshot was taken from."""
        pvcs = self._core.list_namespaced_persistent_volume_claim(
            namespace=_NAMESPACE, label_selector=f"{_SNAPSHOT_LABEL}={snapshot_name}"
        )
        for pvc in pvcs.items:
            source = pvc.metadata.labels.get("firecracker-source-rootfs")
            if source:
                return source
        raise RuntimeError(f"No rootfs PVC found for snapshot {snapshot_name}")

    def delete_snapshot(self, snapshot_name: str) -> None:
        self._delete_if_exists("pvc", _snapshot_pvc_name(snapshot_name))

    def create_restored_vm(
        self,
        *,
        vm_image: str,
        source_rootfs_pvc: str,
        snapshot_name: str,
        cpus: int,
        mem_mib: int,
        node_selector: dict[str, str] | None,
    ) -> VMInfo:
        """Create a VM pod for snapshot restore.

        All PVCs are CoW clones — the source VM's PVCs are never shared:
        - Rootfs: thin clone of source VM's rootfs LV
        - Snapshot: thin clone of the snapshot PVC (has memory + vmstate)
        - Work: fresh thin LV for the restored VM's own use
        """
        vm_id = secrets.token_hex(4)
        rootfs_pvc = self._create_pvc(
            _rootfs_pvc_name(vm_id),
            volume_mode="Block",
            storage_class=_BLOCK_SC,
            size="10Gi",
            labels={_VM_ID_LABEL: vm_id},
            data_source=source_rootfs_pvc,
        )
        # CoW clone of the snapshot PVC — each restored VM gets its own copy.
        snapshot_clone_pvc = self._create_pvc(
            f"firecracker-snap-{vm_id}",
            volume_mode="Filesystem",
            storage_class=_FS_SC,
            size="8Gi",
            labels={_VM_ID_LABEL: vm_id, _SNAPSHOT_LABEL: snapshot_name},
            data_source=_snapshot_pvc_name(snapshot_name),
        )
        work_pvc = self._create_pvc(
            _work_pvc_name(vm_id),
            volume_mode="Filesystem",
            storage_class=_FS_SC,
            size="10Gi",
            labels={_VM_ID_LABEL: vm_id},
        )
        spec = self._pod_spec(
            vm_id,
            vm_image=vm_image,
            rootfs_pvc=rootfs_pvc,
            work_pvc=work_pvc,
            snapshot_pvc=snapshot_clone_pvc,
            cpus=cpus,
            mem_mib=mem_mib,
            node_selector=node_selector,
        )

        logger.info("Creating restored VM pod %s from snapshot %s", _pod_name(vm_id), snapshot_name)
        self._core.create_namespaced_pod(namespace=_NAMESPACE, body=spec)
        return VMInfo(id=vm_id, pod_name=_pod_name(vm_id), status=VMStatus.CREATING)

    # ── Internal ─────────────────────────────────────────────────────────

    def _create_pvc(
        self,
        name: str,
        *,
        volume_mode: str,
        storage_class: str,
        size: str,
        labels: dict[str, str],
        data_source: str | None = None,
    ) -> str:
        spec: dict[str, Any] = {
            "volumeMode": volume_mode,
            "storageClassName": storage_class,
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": size}},
        }
        if data_source:
            spec["dataSource"] = {"kind": "PersistentVolumeClaim", "name": data_source}

        pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": _NAMESPACE, "labels": {_VM_LABEL: _VM_LABEL_VALUE, **labels}},
            "spec": spec,
        }
        logger.info("Creating PVC %s (mode=%s, class=%s)", name, volume_mode, storage_class)
        self._core.create_namespaced_persistent_volume_claim(namespace=_NAMESPACE, body=pvc)
        return name

    def _delete_if_exists(self, kind: str, name: str) -> None:
        try:
            if kind == "pod":
                self._core.delete_namespaced_pod(name=name, namespace=_NAMESPACE)
            elif kind == "pvc":
                self._core.delete_namespaced_persistent_volume_claim(name=name, namespace=_NAMESPACE)
        except client.ApiException as e:
            if e.status != 404:
                raise
            logger.debug("%s %s already deleted", kind, name)

    def _pod_spec(
        self,
        vm_id: str,
        *,
        vm_image: str,
        rootfs_pvc: str,
        work_pvc: str,
        cpus: int,
        mem_mib: int,
        node_selector: dict[str, str] | None,
        snapshot_pvc: str | None = None,
    ) -> dict[str, Any]:
        volume_devices = [{"name": "rootfs", "devicePath": ROOTFS_DEVICE_PATH}]
        volume_mounts = [{"name": "work", "mountPath": WORK_MOUNT}]
        volumes: list[dict[str, Any]] = [
            {"name": "rootfs", "persistentVolumeClaim": {"claimName": rootfs_pvc}},
            {"name": "work", "persistentVolumeClaim": {"claimName": work_pvc}},
        ]

        if snapshot_pvc:
            volume_mounts.append({"name": "snapshot", "mountPath": SNAPSHOT_MOUNT, "readOnly": True})
            volumes.append({"name": "snapshot", "persistentVolumeClaim": {"claimName": snapshot_pvc, "readOnly": True}})

        spec: dict[str, Any] = {
            "containers": [
                {
                    "name": "vm",
                    "image": vm_image,
                    "securityContext": {"capabilities": {"add": ["NET_ADMIN"]}},
                    "resources": {
                        "requests": {"cpu": str(cpus), "memory": f"{mem_mib}Mi", "squat.ai/kvm": "1"},
                        "limits": {"cpu": str(cpus), "memory": f"{mem_mib}Mi", "squat.ai/kvm": "1"},
                    },
                    "ports": [{"containerPort": 2026, "name": "firecracker-api"}],
                    "volumeDevices": volume_devices,
                    "volumeMounts": volume_mounts,
                }
            ],
            "volumes": volumes,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 10,
        }
        if node_selector:
            spec["nodeSelector"] = node_selector

        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": _pod_name(vm_id),
                "namespace": _NAMESPACE,
                "labels": {_VM_LABEL: _VM_LABEL_VALUE, _VM_ID_LABEL: vm_id, "app": "firecracker-vm"},
            },
            "spec": spec,
        }


def _pod_name(vm_id: str) -> str:
    return f"firecracker-vm-{vm_id}"


def _rootfs_pvc_name(vm_id: str) -> str:
    return f"firecracker-rootfs-{vm_id}"


def _work_pvc_name(vm_id: str) -> str:
    return f"firecracker-work-{vm_id}"


def _snapshot_pvc_name(snapshot_name: str) -> str:
    return f"firecracker-snapshot-{snapshot_name}"


def _vm_info_from_pod(pod: client.V1Pod) -> VMInfo:
    vm_id = pod.metadata.labels.get(_VM_ID_LABEL, "unknown")
    phase = VMStatus(pod.status.phase) if pod.status else VMStatus.UNKNOWN
    return VMInfo(id=vm_id, pod_name=pod.metadata.name, status=phase, pod_ip=pod.status.pod_ip if pod.status else None)
