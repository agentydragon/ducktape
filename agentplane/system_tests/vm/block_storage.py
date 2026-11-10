"""Demand-driven loop-backed Block PVs for the VM acceptance stack.

The RBE action exposes loop devices but does not carry losetup. The fixture owns sparse backing files
and the loop fds it attaches, so teardown can detach only those devices after k3s removes its Pods.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import stat
import struct
import threading
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from kubernetes_asyncio import client as k8s
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.driver.cluster import Cluster

STORAGE_CLASS = "agentplane-test-loop-block"
CLAIM_CAPACITY = "10Gi"
_CLAIM_CAPACITY_BYTES = 10 * 1024**3

_LOOP_CTL_GET_FREE = 0x4C82
_LOOP_SET_FD = 0x4C00
_LOOP_CLR_FD = 0x4C01
_LOOP_SET_STATUS64 = 0x4C04
_LOOP_INFO64_SIZE = 232
_LO_FLAGS_AUTOCLEAR = 4
_LO_FLAGS_OFFSET = 52
_ATTACH_ATTEMPTS = 8
_DETACH_TIMEOUT_SECONDS = 30.0
_DETACH_POLL_SECONDS = 0.1


@dataclass
class _LoopDevice:
    index: int
    path: Path
    backing_file: Path
    backing_fd: int
    device_fd: int
    attached: bool = True
    detach_requested: bool = False

    @classmethod
    def attach(cls, backing_file: Path) -> _LoopDevice:
        backing_fd = os.open(backing_file, os.O_RDWR | os.O_CLOEXEC)
        control_fd = os.open("/dev/loop-control", os.O_RDWR | os.O_CLOEXEC)
        try:
            for _ in range(_ATTACH_ATTEMPTS):
                index = fcntl.ioctl(control_fd, _LOOP_CTL_GET_FREE)
                path = Path(f"/dev/loop{index}")
                info = path.stat()
                if not stat.S_ISBLK(info.st_mode) or os.major(info.st_rdev) != 7 or os.minor(info.st_rdev) != index:
                    raise RuntimeError(f"loop-control selected an unexpected device node: {path}")
                device_fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
                try:
                    # LOOP_SET_FD atomically claims a free device. If another fixture races us, retry
                    # only EBUSY; every other failure is actionable and must reach the test.
                    fcntl.ioctl(device_fd, _LOOP_SET_FD, backing_fd)
                except OSError as error:
                    os.close(device_fd)
                    if error.errno == errno.EBUSY:
                        continue
                    raise
                return cls(index, path, backing_file, backing_fd, device_fd)
        except BaseException:
            os.close(backing_fd)
            raise
        finally:
            os.close(control_fd)
        os.close(backing_fd)
        raise RuntimeError(f"could not attach {backing_file}: no free loop device after {_ATTACH_ATTEMPTS} attempts")

    def enable_autoclear(self) -> None:
        """Clear the mapping when the fixture process exits if explicit teardown cannot run."""
        info = bytearray(_LOOP_INFO64_SIZE)
        struct.pack_into("=I", info, _LO_FLAGS_OFFSET, _LO_FLAGS_AUTOCLEAR)
        fcntl.ioctl(self.device_fd, _LOOP_SET_STATUS64, info, True)

    def detach(self) -> None:
        if not self.attached:
            return
        if not self.detach_requested:
            try:
                fcntl.ioctl(self.device_fd, _LOOP_CLR_FD)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise RuntimeError(
                        f"failed to detach fixture-owned {self.path} backed by {self.backing_file}"
                    ) from error
                # Someone may have detached our loop already. Do not issue another clear: that device number
                # could have been reassigned to another owner between those operations.
            self.detach_requested = True
            device_fd, self.device_fd = self.device_fd, -1
            backing_fd, self.backing_fd = self.backing_fd, -1
            os.close(device_fd)
            os.close(backing_fd)

        # LOOP_CLR_FD honors AUTOCLEAR when a pod still has the block device open. Drop our descriptors
        # before checking sysfs so that only external users can keep the mapping alive.
        sysfs_backing_file = Path(f"/sys/block/loop{self.index}/loop/backing_file")
        for attempt in Retrying(
            stop=stop_after_delay(_DETACH_TIMEOUT_SECONDS),
            wait=wait_fixed(_DETACH_POLL_SECONDS),
            retry=retry_if_exception_type(AssertionError),
            reraise=True,
        ):
            with attempt:
                try:
                    backing_file = sysfs_backing_file.read_text().strip()
                except FileNotFoundError:
                    backing_file = ""
                assert backing_file != str(self.backing_file), (
                    f"fixture loop remained attached after teardown: {self.path}"
                )
        self.attached = False


class BlockStorage:
    """Workspace volumes owned until k3s stops, including volumes of deleted claims."""

    def __init__(self, work: Path) -> None:
        self.work = work / "raw-block"
        self.devices: list[_LoopDevice] = []
        self.closed = False

    def __enter__(self) -> BlockStorage:
        self.work.mkdir()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def provisioning(self, cluster: Cluster, *, node_name: str) -> Iterator[None]:
        # Session fixtures outlive pytest's per-test event loops. Own this worker's loop so
        # provisioning continues during setup, between cases, and through Sandbox cleanup.
        stopped = threading.Event()
        ready: Future[None] = Future()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace-provisioner") as executor:
            worker = executor.submit(lambda: asyncio.run(self._serve(cluster, node_name, stopped, ready)))
            try:
                ready.result(timeout=15)
                yield
            finally:
                stopped.set()
                worker.result()

    async def _serve(self, cluster: Cluster, node_name: str, stopped: threading.Event, ready: Future[None]) -> None:
        try:
            async with cluster.api_client() as api:
                await k8s.StorageV1Api(api).create_storage_class(
                    k8s.V1StorageClass(
                        metadata=k8s.V1ObjectMeta(name=STORAGE_CLASS),
                        provisioner="kubernetes.io/no-provisioner",
                        volume_binding_mode="WaitForFirstConsumer",
                    ),
                    _request_timeout=10,
                )
                core = k8s.CoreV1Api(api)
                provisioned: set[str] = set()
                while not stopped.is_set():
                    # The disposable namespace has only a handful of claims. Relisting also
                    # handles claim replacement without watch reconnect/resourceVersion state.
                    claims = await core.list_namespaced_persistent_volume_claim(cluster.namespace, _request_timeout=10)
                    for claim in claims.items:
                        if (
                            claim.spec.storage_class_name != STORAGE_CLASS
                            or claim.spec.volume_name
                            or claim.metadata.deletion_timestamp is not None
                            or claim.metadata.uid in provisioned
                        ):
                            continue
                        await self._provision(core, claim, node_name=node_name)
                        provisioned.add(claim.metadata.uid)
                    if not ready.done():
                        ready.set_result(None)
                    await asyncio.sleep(0.25)
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    async def _provision(self, core: k8s.CoreV1Api, claim: k8s.V1PersistentVolumeClaim, *, node_name: str) -> None:
        if (
            claim.spec.volume_mode != "Block"
            or claim.spec.access_modes != ["ReadWriteOnce"]
            or claim.spec.resources.requests["storage"] != CLAIM_CAPACITY
        ):
            raise ValueError(f"unsupported workspace claim: {claim.metadata.namespace}/{claim.metadata.name}")
        backing_file = self.work / f"{claim.metadata.uid}.img"
        with backing_file.open("xb") as backing:
            backing.truncate(_CLAIM_CAPACITY_BYTES)
        device = _LoopDevice.attach(backing_file)
        self.devices.append(device)
        device.enable_autoclear()
        await core.create_persistent_volume(
            k8s.V1PersistentVolume(
                metadata=k8s.V1ObjectMeta(name=f"agentplane-block-{claim.metadata.uid}"),
                spec=k8s.V1PersistentVolumeSpec(
                    capacity={"storage": CLAIM_CAPACITY},
                    volume_mode="Block",
                    access_modes=["ReadWriteOnce"],
                    persistent_volume_reclaim_policy="Retain",
                    storage_class_name=STORAGE_CLASS,
                    local=k8s.V1LocalVolumeSource(path=str(device.path)),
                    # Bind to the UID: deleting and recreating a claim must never reuse its data.
                    claim_ref=k8s.V1ObjectReference(
                        api_version="v1",
                        kind="PersistentVolumeClaim",
                        name=claim.metadata.name,
                        namespace=claim.metadata.namespace,
                        uid=claim.metadata.uid,
                    ),
                    node_affinity=k8s.V1VolumeNodeAffinity(
                        required=k8s.V1NodeSelector(
                            node_selector_terms=[
                                k8s.V1NodeSelectorTerm(
                                    match_expressions=[
                                        k8s.V1NodeSelectorRequirement(
                                            key="kubernetes.io/hostname", operator="In", values=[node_name]
                                        )
                                    ]
                                )
                            ]
                        )
                    ),
                ),
            ),
            _request_timeout=10,
        )

    def close(self) -> None:
        if self.closed:
            return
        errors: list[Exception] = []
        for device in reversed(self.devices):
            try:
                device.detach()
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("raw block fixture could not release its loop devices", errors)
        self.closed = True
