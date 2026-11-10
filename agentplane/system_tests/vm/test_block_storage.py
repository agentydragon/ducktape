"""Exercise provisioning against a real apiserver and the executor's loop devices."""

import asyncio
import os
import stat
from pathlib import Path

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.vm.block_storage import CLAIM_CAPACITY, STORAGE_CLASS, BlockStorage
from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver


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


if __name__ == "__main__":
    pytest_bazel.main()
