"""Firecracker VM manager — FastAPI service.

Orchestrates Firecracker VM pods via the Kubernetes API. The manager is
the VMM brain: it creates pods (infrastructure), then drives boot or
restore through the Firecracker API proxy on each pod.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from devinfra.firecracker.manager.clients import FirecrackerClient, ProcessApiControl
from devinfra.firecracker.manager.config import ManagerConfig, load_config
from devinfra.firecracker.manager.k8s_client import INITRAMFS_PATH, KERNEL_PATH, ROOTFS_DEVICE_PATH, K8sVMClient
from devinfra.firecracker.manager.models import (
    CreateVMRequest,
    CreateVMResponse,
    ListVMsResponse,
    RestoreRequest,
    SnapshotRequest,
    VMInfo,
)
from devinfra.firecracker.manager.snapshots import snapshot_vm

logger = logging.getLogger(__name__)

_auth_scheme = HTTPBearer()
_AUTH_TOKEN = os.environ.get("FC_MANAGER_AUTH_TOKEN", "")


async def _require_auth(request: Request) -> None:
    if not _AUTH_TOKEN:
        return
    credentials: HTTPAuthorizationCredentials | None = await _auth_scheme(request)
    if credentials is None or not secrets.compare_digest(credentials.credentials, _AUTH_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


app = FastAPI(title="Firecracker VM Manager", dependencies=[Depends(_require_auth)])


# ── Dependencies ─────────────────────────────────────────────────────────────


def _config(request: Request) -> ManagerConfig:
    return request.app.state.config


def _k8s(request: Request) -> K8sVMClient:
    return request.app.state.k8s


def _require_vm_with_ip(vm_id: str, k8s: K8s) -> VMInfo:
    vm = k8s.get_vm(vm_id)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} not found")
    if vm.pod_ip is None:
        raise HTTPException(status_code=409, detail=f"VM {vm_id} has no pod IP yet")
    return vm


def _firecracker_client(vm: RunningVM) -> Iterator[FirecrackerClient]:
    client = FirecrackerClient(vm.pod_ip)
    try:
        yield client
    finally:
        client.close()


def _control_client(vm: RunningVM) -> Iterator[ProcessApiControl]:
    client = ProcessApiControl(vm.pod_ip)
    try:
        yield client
    finally:
        client.close()


Config = Annotated[ManagerConfig, Depends(_config)]
K8s = Annotated[K8sVMClient, Depends(_k8s)]
RunningVM = Annotated[VMInfo, Depends(_require_vm_with_ip)]
Firecracker = Annotated[FirecrackerClient, Depends(_firecracker_client)]
Control = Annotated[ProcessApiControl, Depends(_control_client)]


# ── Endpoints ────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/vms")
async def create_vm(req: CreateVMRequest, config: Config, k8s: K8s) -> CreateVMResponse:
    """Create a new Firecracker VM: PVCs + pod."""
    vm = k8s.create_vm(
        vm_image=config.vm_image,
        base_rootfs_pvc=config.base_rootfs_pvc,
        cpus=req.cpus,
        mem_mib=req.mem_mib,
        node_selector=config.node_selector,
    )
    return CreateVMResponse(vm=vm)


@app.post("/vms/{vm_id}/boot")
async def boot(vm_id: str, firecracker: Firecracker, control: Control) -> dict[str, str]:
    """Boot a VM that has a running pod but hasn't been started yet."""
    firecracker.wait_ready()
    firecracker.boot(kernel_path=KERNEL_PATH, rootfs_path=ROOTFS_DEVICE_PATH, initramfs_path=INITRAMFS_PATH)
    control.wait_ready()
    return {"status": "booted", "vm_id": vm_id}


@app.get("/vms")
async def list_vms(k8s: K8s) -> ListVMsResponse:
    return ListVMsResponse(vms=k8s.list_vms())


@app.get("/vms/{vm_id}")
async def get_vm(vm_id: str, k8s: K8s) -> VMInfo:
    vm = k8s.get_vm(vm_id)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} not found")
    return vm


@app.delete("/vms/{vm_id}")
async def destroy_vm(vm_id: str, k8s: K8s) -> dict[str, str]:
    k8s.delete_vm(vm_id)
    return {"status": "deleted", "vm_id": vm_id}


@app.post("/vms/{vm_id}/snapshot")
async def snapshot(
    vm_id: str, req: SnapshotRequest, firecracker: Firecracker, control: Control, k8s: K8s
) -> dict[str, str]:
    snapshot_vm(firecracker, control, snapshot_name=req.name)
    # Record source VM's rootfs PVC for restore cloning.
    k8s.label_snapshot(vm_id, req.name)
    return {"status": "snapshot_created", "name": req.name, "vm_id": vm_id}


@app.post("/snapshots/{snapshot_name}/restore")
async def restore(snapshot_name: str, req: RestoreRequest, config: Config, k8s: K8s) -> CreateVMResponse:
    """Create a new VM restored from a snapshot."""
    source_rootfs_pvc = k8s.get_snapshot_rootfs_pvc(snapshot_name)
    vm = k8s.create_restored_vm(
        vm_image=config.vm_image,
        source_rootfs_pvc=source_rootfs_pvc,
        snapshot_name=snapshot_name,
        cpus=req.cpus,
        mem_mib=req.mem_mib,
        node_selector=config.node_selector,
    )
    # TODO: wait for pod to be Running, then call restore_vm().
    return CreateVMResponse(vm=vm)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/etc/firecracker-manager/config.yaml")
    logger.info("Loading config from %s", config_path)

    config = load_config(config_path)
    app.state.config = config
    app.state.k8s = K8sVMClient()

    logger.info("Starting Firecracker VM manager on port %d", config.port)
    uvicorn.run(app, host="0.0.0.0", port=config.port)


if __name__ == "__main__":
    main()
