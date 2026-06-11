"""Snapshot and restore operations for Firecracker VMs.

All operations go through the pod IP — the entrypoint proxies guest ports
(2024, 2025) and the Firecracker API socket (2026) onto the pod network.

Snapshot:
  1. Manager creates a snapshot PVC (filesystem, thin-provisioned)
  2. control.freeze_filesystem() — flush writes, freeze guest fs
  3. firecracker.pause()
  4. firecracker.create_snapshot() — writes memory + vmstate to /work/snapshots/
  5. control.thaw_filesystem() + firecracker.resume()
  Note: snapshot files live on the VM's work PVC. The separate snapshot
  PVC is for restore — a Job copies files from work PVC to snapshot PVC
  so the source VM's work PVC isn't tied up.

Restore:
  1. Manager creates a new pod with snapshot PVC mounted at /snapshots
  2. firecracker.load_snapshot() — loads from /snapshots/<name>/
  3. control.thaw_filesystem()
"""

from __future__ import annotations

import logging

from devinfra.firecracker.manager.clients import FirecrackerClient, ProcessApiControl
from devinfra.firecracker.manager.k8s_client import SNAPSHOT_MOUNT, WORK_MOUNT

logger = logging.getLogger(__name__)


def snapshot_vm(firecracker: FirecrackerClient, control: ProcessApiControl, *, snapshot_name: str) -> None:
    """Snapshot a running VM. Files are written to the work PVC."""
    snapshot_dir = f"{WORK_MOUNT}/snapshots/{snapshot_name}"

    logger.info("Freezing guest filesystem")
    control.freeze_filesystem()

    try:
        logger.info("Pausing VM")
        firecracker.pause()

        logger.info("Creating snapshot %s", snapshot_name)
        firecracker.create_snapshot(snapshot_path=f"{snapshot_dir}/vmstate", mem_file_path=f"{snapshot_dir}/memory")
    finally:
        logger.info("Thawing guest filesystem")
        control.thaw_filesystem()

    logger.info("Resuming VM")
    firecracker.resume()


def restore_vm(firecracker: FirecrackerClient, control: ProcessApiControl, *, snapshot_name: str) -> None:
    """Restore a VM from a snapshot PVC mounted at /snapshots."""
    snapshot_dir = f"{SNAPSHOT_MOUNT}/{snapshot_name}"

    firecracker.wait_ready()
    firecracker.load_snapshot(snapshot_path=f"{snapshot_dir}/vmstate", mem_file_path=f"{snapshot_dir}/memory")
    control.wait_ready()
    control.thaw_filesystem()
