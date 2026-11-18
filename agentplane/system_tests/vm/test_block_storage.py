"""Exercise provisioning against a real apiserver and the executor's loop devices."""

import asyncio
import errno
import fcntl
import os
import stat
import struct
import threading
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.vm import block_storage
from agentplane.system_tests.vm.block_storage import CLAIM_CAPACITY, STORAGE_CLASS, BlockStorage
from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver

_LOOP_GET_STATUS64 = 0x4C05
_LOOP_CLR_FD = 0x4C01
_LOOP_INFO64_SIZE = 232
_LOOP_FLAGS_OFFSET = 52
_LO_FLAGS_AUTOCLEAR = 4


def workspace_claim(name: str, *, storage_class: str = STORAGE_CLASS) -> k8s.V1PersistentVolumeClaim:
    return k8s.V1PersistentVolumeClaim(
        metadata=k8s.V1ObjectMeta(name=name),
        spec=k8s.V1PersistentVolumeClaimSpec(
            storage_class_name=storage_class,
            volume_mode="Block",
            access_modes=["ReadWriteOnce"],
            resources=k8s.V1VolumeResourceRequirements(requests={"storage": CLAIM_CAPACITY}),
        ),
    )


async def wait_for_volumes(core: k8s.CoreV1Api, count: int) -> list[k8s.V1PersistentVolume]:
    async for attempt in AsyncRetrying(
        retry=retry_if_exception_type(AssertionError), stop=stop_after_delay(20), wait=wait_fixed(0.05), reraise=True
    ):
        with attempt:
            volumes = await core.list_persistent_volume()
            assert len(volumes.items) == count
    return list(volumes.items)


def write_marker(path: Path) -> None:
    with path.open("r+b", buffering=0) as device:
        device.write(b"retained workspace")
        os.fsync(device.fileno())


def read_marker(path: Path) -> bytes:
    with path.open("rb", buffering=0) as device:
        return device.read(18)


def is_block_device(path: str) -> bool:
    return stat.S_ISBLK(Path(path).stat().st_mode)


def loop_status64(fd: int) -> tuple[int, int, int, int]:
    info = bytearray(_LOOP_INFO64_SIZE)
    fcntl.ioctl(fd, _LOOP_GET_STATUS64, info, True)
    lo_device, lo_inode, lo_rdevice = struct.unpack_from("=QQQ", info)
    lo_flags = struct.unpack_from("=I", info, _LOOP_FLAGS_OFFSET)[0]
    return lo_device, lo_inode, lo_rdevice, lo_flags


def encode_loop_device_number(device: int) -> int:
    major = os.major(device)
    minor = os.minor(device)
    return (minor & 0xFF) | (major << 8) | ((minor & ~0xFF) << 12)


def loop_backing_file(index: int) -> str:
    try:
        return Path(f"/sys/block/loop{index}/loop/backing_file").read_text().strip()
    except FileNotFoundError:
        return ""


def clear_loop_if_same(path: Path, expected: tuple[int, int, int, int]) -> None:
    """Best-effort test cleanup guarded by the lease's captured kernel identity."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENXIO):
            return
        raise
    try:
        try:
            actual = loop_status64(fd)
        except OSError as error:
            if error.errno == errno.ENXIO:
                return
            raise
        if actual != expected:
            return
        fcntl.ioctl(fd, _LOOP_CLR_FD)
    finally:
        os.close(fd)


def test_detach_without_kernel_lease_preserves_backing_data(tmp_path: Path) -> None:
    backing_file = tmp_path / "ordinary.img"
    backing_file.write_bytes(bytes(1024 * 1024))
    device = block_storage._LoopDevice.attach(backing_file)
    try:
        write_marker(device.path)
        device.detach()
        assert loop_backing_file(device.index) != str(backing_file)
        assert read_marker(backing_file) == b"retained workspace"
    finally:
        if device.attached:
            device.detach()


def test_detach_releases_kernel_loop_lease_and_preserves_unrelated_loop(tmp_path: Path) -> None:
    backing_file = tmp_path / "owned.img"
    backing_file.write_bytes(bytes(1024 * 1024))
    unrelated_backing = tmp_path / "unrelated.img"
    unrelated_backing.write_bytes(bytes(1024 * 1024))

    owned = block_storage._LoopDevice.attach(backing_file)
    lease: block_storage._LoopDevice | None = None
    lease_status: tuple[int, int, int, int] | None = None
    unrelated: block_storage._LoopDevice | None = None
    try:
        write_marker(owned.path)
        owned_device = os.fstat(owned.device_fd).st_rdev

        # Kubelet's loop-backed local block volume holds the original loop as another loop's
        # backing device. Close both setup FDs so only that kernel dependency remains.
        lease = block_storage._LoopDevice.attach(owned.path)
        lease_status = loop_status64(lease.device_fd)
        assert lease_status[2] == encode_loop_device_number(owned_device)
        assert lease_status[3] & _LO_FLAGS_AUTOCLEAR == 0
        os.close(lease.device_fd)
        lease.device_fd = -1
        os.close(lease.backing_fd)
        lease.backing_fd = -1

        unrelated = block_storage._LoopDevice.attach(unrelated_backing)
        write_marker(unrelated.path)
        assert loop_backing_file(lease.index) == str(owned.path)
        assert loop_backing_file(unrelated.index) == str(unrelated_backing)

        owned.detach()

        assert loop_backing_file(owned.index) != str(backing_file)
        assert loop_backing_file(lease.index) != str(owned.path)
        assert read_marker(backing_file) == b"retained workspace"
        assert loop_backing_file(unrelated.index) == str(unrelated_backing)
        assert read_marker(unrelated_backing) == b"retained workspace"
    finally:
        try:
            if lease is not None:
                if lease.device_fd >= 0:
                    lease.detach()
                elif lease_status is not None:
                    clear_loop_if_same(lease.path, lease_status)
        finally:
            try:
                if owned.attached:
                    owned.detach()
            finally:
                if unrelated is not None and unrelated.attached:
                    unrelated.detach()


def test_detach_rejects_a_changed_backing_inode_without_clearing(tmp_path: Path) -> None:
    backing_file = tmp_path / "owned.img"
    backing_file.write_bytes(bytes(1024 * 1024))
    different_file = tmp_path / "different.img"
    different_file.write_bytes(bytes(1024 * 1024))
    device = block_storage._LoopDevice.attach(backing_file)
    owned_backing_fd = device.backing_fd
    different_backing_fd = os.open(different_file, os.O_RDWR | os.O_CLOEXEC)
    try:
        device.backing_fd = different_backing_fd
        with pytest.raises(RuntimeError, match="refusing to detach changed"):
            device.detach()
        assert device.attached
        assert not device.detach_requested
        assert loop_backing_file(device.index) == str(backing_file)
    finally:
        device.backing_fd = owned_backing_fd
        os.close(different_backing_fd)
        if device.attached:
            device.detach()


async def test_concurrent_claims_and_replacement_have_independent_retained_devices(tmp_path: Path) -> None:
    async with kubernetes_apiserver() as server:
        core = k8s.CoreV1Api(server.api)
        await core.create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name="workspace-test")))
        cluster = Cluster(kubeconfig=server.kubeconfig, namespace="workspace-test", kubectl_command=("unused-kubectl",))
        await core.create_namespaced_persistent_volume_claim(
            cluster.namespace, workspace_claim("other-storage", storage_class="unrelated-class")
        )
        with BlockStorage(tmp_path) as storage:
            with storage.provisioning(cluster, node_name="test-node"):
                first, second = await asyncio.gather(
                    core.create_namespaced_persistent_volume_claim(cluster.namespace, workspace_claim("first")),
                    core.create_namespaced_persistent_volume_claim(cluster.namespace, workspace_claim("second")),
                )
                volumes = await wait_for_volumes(core, 2)
                by_claim = {volume.spec.claim_ref.uid: volume for volume in volumes}
                assert set(by_claim) == {first.metadata.uid, second.metadata.uid}
                paths = {volume.spec.local.path for volume in volumes}
                assert len(paths) == 2
                assert all(await asyncio.gather(*(asyncio.to_thread(is_block_device, path) for path in paths)))
                for volume in volumes:
                    assert volume.spec.claim_ref.namespace == cluster.namespace
                    assert volume.spec.node_affinity.required.node_selector_terms[0].match_expressions[0].values == [
                        "test-node"
                    ]
                first_device = Path(by_claim[first.metadata.uid].spec.local.path)
                await asyncio.to_thread(write_marker, first_device)
                await core.delete_namespaced_persistent_volume_claim(first.metadata.name, cluster.namespace)
                # Envtest has no controller-manager to remove the unused claim's protection finalizer.
                await core.patch_namespaced_persistent_volume_claim(
                    first.metadata.name, cluster.namespace, [{"op": "remove", "path": "/metadata/finalizers"}]
                )
                replacement = await core.create_namespaced_persistent_volume_claim(
                    cluster.namespace, workspace_claim("first")
                )
                volumes = await wait_for_volumes(core, 3)
                by_claim = {volume.spec.claim_ref.uid: volume for volume in volumes}
                assert set(by_claim) == {first.metadata.uid, second.metadata.uid, replacement.metadata.uid}
                assert len({volume.spec.local.path for volume in volumes}) == 3
                assert await asyncio.to_thread(
                    read_marker, Path(by_claim[replacement.metadata.uid].spec.local.path)
                ) == bytes(18)
                assert await asyncio.to_thread(read_marker, first_device) == b"retained workspace"
            # The provisioner stops before the devices are released, just as it does before k3s stops.
            backing_files = {
                Path(f"/sys/block/{Path(volume.spec.local.path).name}/loop/backing_file"): str(
                    tmp_path / "raw-block" / f"{volume.spec.claim_ref.uid}.img"
                )
                for volume in volumes
            }
        assert all(not path.exists() or path.read_text().strip() != backing for path, backing in backing_files.items())


@pytest.mark.parametrize(
    "transient_error",
    [TimeoutError("injected PVC list deadline"), aiohttp.ClientConnectionError("injected PVC list disconnect")],
    ids=["deadline", "transport"],
)
async def test_transient_claim_list_error_recovers_without_reprovisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient_error: Exception
) -> None:
    async with kubernetes_apiserver() as server:
        core = k8s.CoreV1Api(server.api)
        await core.create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name="workspace-test")))
        cluster = Cluster(kubeconfig=server.kubeconfig, namespace="workspace-test", kubectl_command=("unused-kubectl",))
        claims = await asyncio.gather(
            core.create_namespaced_persistent_volume_claim(cluster.namespace, workspace_claim("first")),
            core.create_namespaced_persistent_volume_claim(cluster.namespace, workspace_claim("second")),
        )

        list_calls = 0
        storage_class_creates = 0
        volume_creates = 0
        attach_calls = 0
        failed_list = threading.Event()
        recovered_list = threading.Event()
        original_list = k8s.CoreV1Api.list_namespaced_persistent_volume_claim
        original_create_storage_class = k8s.StorageV1Api.create_storage_class
        original_create_volume = k8s.CoreV1Api.create_persistent_volume
        original_attach = block_storage._LoopDevice.attach

        async def fail_second_list(self: k8s.CoreV1Api, *args: Any, **kwargs: Any):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 2:
                failed_list.set()
                raise transient_error
            result = await original_list(self, *args, **kwargs)
            if list_calls >= 3:
                recovered_list.set()
            return result

        async def count_storage_class_creates(self: k8s.StorageV1Api, *args: Any, **kwargs: Any):
            nonlocal storage_class_creates
            storage_class_creates += 1
            return await original_create_storage_class(self, *args, **kwargs)

        async def count_volume_creates(self: k8s.CoreV1Api, *args: Any, **kwargs: Any):
            nonlocal volume_creates
            volume_creates += 1
            return await original_create_volume(self, *args, **kwargs)

        def count_attaches(backing_file: Path):
            nonlocal attach_calls
            attach_calls += 1
            return original_attach(backing_file)

        monkeypatch.setattr(k8s.CoreV1Api, "list_namespaced_persistent_volume_claim", fail_second_list)
        monkeypatch.setattr(k8s.StorageV1Api, "create_storage_class", count_storage_class_creates)
        monkeypatch.setattr(k8s.CoreV1Api, "create_persistent_volume", count_volume_creates)
        monkeypatch.setattr(block_storage._LoopDevice, "attach", count_attaches)

        with BlockStorage(tmp_path) as storage, storage.provisioning(cluster, node_name="test-node"):
            volumes = await wait_for_volumes(core, 2)
            assert await asyncio.to_thread(failed_list.wait, 5)
            assert await asyncio.to_thread(recovered_list.wait, 5)
            assert {volume.spec.claim_ref.uid for volume in volumes} == {claim.metadata.uid for claim in claims}
            assert storage_class_creates == 1
            assert volume_creates == 2
            assert attach_calls == 2
            assert len(storage.devices) == 2


async def test_claim_list_retry_exhaustion_and_stop_preserve_control_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    async with kubernetes_apiserver() as server:
        core = k8s.CoreV1Api(server.api)
        attempts: list[TimeoutError] = []

        async def timeout_list(self: k8s.CoreV1Api, _namespace: str, **_kwargs: Any):
            error = TimeoutError(f"injected PVC list timeout {len(attempts) + 1}")
            attempts.append(error)
            raise error

        monkeypatch.setattr(k8s.CoreV1Api, "list_namespaced_persistent_volume_claim", timeout_list)
        stopped = threading.Event()
        with pytest.raises(TimeoutError) as caught:
            await block_storage._list_claims(core, "workspace-test", stopped)

        assert len(attempts) == block_storage._CLAIM_LIST_ATTEMPTS
        assert caught.value is attempts[-1]

        stopped.set()
        assert await block_storage._list_claims(core, "workspace-test", stopped) is None
        assert len(attempts) == block_storage._CLAIM_LIST_ATTEMPTS

        stop_after_error_calls = 0
        stopped.clear()

        async def timeout_and_stop(self: k8s.CoreV1Api, _namespace: str, **_kwargs: Any):
            nonlocal stop_after_error_calls
            stop_after_error_calls += 1
            stopped.set()
            raise TimeoutError("injected PVC list timeout during stop")

        monkeypatch.setattr(k8s.CoreV1Api, "list_namespaced_persistent_volume_claim", timeout_and_stop)
        assert await block_storage._list_claims(core, "workspace-test", stopped) is None
        assert stop_after_error_calls == 1

        started = asyncio.Event()
        cancellation_calls = 0
        stopped.clear()

        async def hanging_list(self: k8s.CoreV1Api, _namespace: str, **_kwargs: Any):
            nonlocal cancellation_calls
            cancellation_calls += 1
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(k8s.CoreV1Api, "list_namespaced_persistent_volume_claim", hanging_list)
        task = asyncio.create_task(block_storage._list_claims(core, "workspace-test", stopped))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancellation_calls == 1


async def test_forbidden_claim_list_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with kubernetes_apiserver() as server:
        core = k8s.CoreV1Api(server.api)
        await core.create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name="workspace-test")))
        cluster = Cluster(kubeconfig=server.kubeconfig, namespace="workspace-test", kubectl_command=("unused-kubectl",))
        list_calls = 0

        async def forbidden_list(self: k8s.CoreV1Api, *args: Any, **kwargs: Any):
            nonlocal list_calls
            list_calls += 1
            raise ApiException(status=403, reason="Forbidden")

        monkeypatch.setattr(k8s.CoreV1Api, "list_namespaced_persistent_volume_claim", forbidden_list)
        with (
            BlockStorage(tmp_path) as storage,
            pytest.raises(ApiException) as caught,
            storage.provisioning(cluster, node_name="test-node"),
        ):
            pytest.fail("provisioner swallowed a forbidden PVC list")

        assert caught.value.status == 403
        assert list_calls == 1


async def test_worker_failure_reaches_the_fixture_owner(tmp_path: Path) -> None:
    async with kubernetes_apiserver() as server:
        core = k8s.CoreV1Api(server.api)
        await core.create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name="workspace-test")))
        cluster = Cluster(kubeconfig=server.kubeconfig, namespace="workspace-test", kubectl_command=("unused-kubectl",))
        claim = workspace_claim("unsupported")
        claim.spec.volume_mode = "Filesystem"
        await core.create_namespaced_persistent_volume_claim(cluster.namespace, claim)
        with BlockStorage(tmp_path) as storage:
            with (
                pytest.raises(ValueError, match="unsupported workspace claim"),
                storage.provisioning(cluster, node_name="test-node"),
            ):
                pytest.fail("provisioner accepted an unsupported workspace claim")
            assert not (await core.list_persistent_volume()).items
