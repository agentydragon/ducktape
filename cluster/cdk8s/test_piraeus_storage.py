"""Storage admission, retention and network access at the Piraeus and descheduler chart boundaries."""

import tomllib
from collections.abc import Callable
from typing import Any

import pytest
import pytest_bazel
import yaml
from cdk8s import App, Chart

from cluster.cdk8s import descheduler, piraeus


@pytest.fixture(scope="module")
def cluster_chart(scratch_app_factory: Callable[[], App]) -> Chart:
    return piraeus.cluster_chart(scratch_app_factory())


@pytest.fixture(scope="module")
def values(cluster_chart: Chart) -> dict[str, Any]:
    release = next(
        obj for obj in cluster_chart.api_objects if obj.kind == "HelmRelease" and obj.name == "linstor-cluster"
    )
    values = release.to_json()["spec"]["values"]
    assert isinstance(values, dict)
    return values


def test_piraeus_storage_is_confined_to_prepared_nodes(values: dict[str, Any]) -> None:
    controller_selector = {"storage.allegedly.works/piraeus-ready": "true", "topology.kubernetes.io/region": "hil"}

    assert values["createInternalTLS"] == "cert-manager"
    assert values["createApiTLS"] == "cert-manager"
    cluster = values["linstorCluster"]
    assert "nodeSelector" not in cluster
    assert cluster["nodeAffinity"] == {
        "nodeSelectorTerms": [
            {
                "matchExpressions": [
                    {"key": "storage.allegedly.works/piraeus-ready", "operator": "In", "values": ["true"]}
                ]
            }
        ]
    }
    for component in ("controller", "csiController", "affinityController"):
        assert cluster[component]["podTemplate"]["spec"]["nodeSelector"] == controller_selector
    assert cluster["properties"] == [{"name": "TcpPortAutoRange", "value": "7900-7999"}]
    assert cluster["highAvailabilityController"] == {"enabled": True}
    assert cluster["nfsServer"] == {"enabled": False}

    satellites = values["linstorSatelliteConfigurations"]
    assert satellites
    [reactor_patch] = satellites[0]["patches"]
    assert reactor_patch["target"] == {"version": "v1", "kind": "ConfigMap", "name": "reactor-config"}
    # Scope the patch to Reactor's metrics configuration. The satellite's LINSTOR listener
    # is managed separately; a stray bind directive there can disconnect the node.
    patch = yaml.safe_load(reactor_patch["patch"])
    assert patch["metadata"]["name"] == reactor_patch["target"]["name"]
    assert set(patch["data"]) == {"prometheus.toml"}
    (listener,) = tomllib.loads(patch["data"]["prometheus.toml"])["prometheus"]
    assert listener["address"] == "127.0.0.1:9942"

    # source.hostDevices initializes the whole selected device. This closed admission
    # list protects physical/host-provisioning identities:
    # OVH uses separate data disks, OptiPlex a Talos raw partition, Wyrm2 hotplugged
    # Proxmox disks, and Rugged an unlocked LUKS partition.
    expected_devices = {
        "ovh-ns1001419": {"ssd": "/dev/disk/by-id/nvme-WDC_CL_SN720_SDAQNTW-512G-2000_1951A3449505"},
        "ovh-ns104952": {"ssd": "/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8304019P450RGN"},
        "ovh-ns104963": {"ssd": "/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8256009U450RGN"},
        "optiplex": {"ssd": "/dev/disk/by-partlabel/r-piraeus"},
        "ovh-ns103656": {"hdd": "/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H8UMLA"},
        "ovh-ns103711": {"hdd": "/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H48V6A"},
        "ovh-ns102453": {"hdd": "/dev/disk/by-id/ata-HGST_HUS726T4TALA6L1_V3H370PN"},
        "wyrm2": {"ssd": "/dev/disk/by-id/virtio-piraeus-ssd", "hdd": "/dev/disk/by-id/virtio-piraeus-hdd"},
        "rugged": {"ssd": "/dev/mapper/piraeus-ssd"},
    }
    actual_devices: dict[str, dict[str, str]] = {}
    for satellite in satellites:
        selector = satellite["nodeSelector"]
        assert selector["storage.allegedly.works/piraeus-ready"] == "true"
        hostname = selector["kubernetes.io/hostname"]
        assert hostname not in actual_devices
        assert satellite["deletionPolicy"] == "Retain"
        assert satellite["patches"] == satellites[0]["patches"]
        pools = satellite["storagePools"]
        assert pools
        assert len({pool["name"] for pool in pools}) == len(pools)
        actual_devices[hostname] = {}
        for pool in pools:
            name = pool["name"]
            assert pool["lvmThinPool"]["volumeGroup"] == f"piraeus_{name}"
            assert pool["lvmThinPool"]["thinPool"] == "data"
            assert pool["lvmThinPool"]["logicalVolumeCreateArguments"] == [
                "--poolmetadatasize",
                "4G",
                "--chunksize",
                "64K",
            ]
            [device] = pool["source"]["hostDevices"]
            actual_devices[hostname][name] = device
    assert actual_devices == expected_devices

    classes = {storage_class["name"]: storage_class for storage_class in values["storageClasses"]}
    assert len(classes) == len(values["storageClasses"])
    # StorageClass names and pool types are a deployment contract. Every class must
    # resolve to a pool on a prepared node.
    # Each entry gives the promised pool, reclaim policy and replica count.
    expected = {
        "piraeus-ssd-local": ("ssd", "Retain", "1"),
        "piraeus-ssd-local-delete": ("ssd", "Delete", "1"),
        "piraeus-hdd-local": ("hdd", "Retain", "1"),
        "piraeus-ssd-2-autodiskful": ("ssd", "Retain", "2"),
        "piraeus-hdd-2": ("hdd", "Retain", "2"),
    }
    assert classes.keys() == expected.keys()
    available_pools = {name for pools in actual_devices.values() for name in pools}
    for name, storage_class in classes.items():
        pool, reclaim_policy, replicas = expected[name]
        assert storage_class["reclaimPolicy"] == reclaim_policy
        assert storage_class["volumeBindingMode"] == "WaitForFirstConsumer"
        assert storage_class["allowVolumeExpansion"] is True
        parameters = storage_class["parameters"]
        assert parameters["linstor.csi.linbit.com/storagePool"] == pool
        assert pool in available_pools
        assert parameters["linstor.csi.linbit.com/placementCount"] == replicas
        if replicas == "1":
            assert parameters["linstor.csi.linbit.com/allowRemoteVolumeAccess"] == "false"
        else:
            # Replicated copies and attachments stay inside OVH, never across the home link.
            assert parameters["linstor.csi.linbit.com/replicasOnSame"] == "topology.kubernetes.io/zone=hil-ovh"
            assert yaml.safe_load(parameters["linstor.csi.linbit.com/allowRemoteVolumeAccess"]) == [
                {"fromSame": ["topology.kubernetes.io/zone"]}
            ]
            assert parameters["DrbdOptions/auto-quorum"] == "suspend-io"
            assert parameters["DrbdOptions/auto-diskful"] == "1"
            assert parameters["DrbdOptions/auto-diskful-allow-cleanup"] == "true"


def test_descheduler_protects_piraeus_node_local_storage_classes(scratch_app: App, values: dict[str, Any]) -> None:
    release = next(obj for obj in descheduler.chart(scratch_app).api_objects if obj.kind == "HelmRelease")
    [default_profile] = release.to_json()["spec"]["values"]["deschedulerPolicy"]["profiles"]
    default_evictor = next(config for config in default_profile["pluginConfig"] if config["name"] == "DefaultEvictor")
    protections = default_evictor["args"]["podProtections"]
    assert "PodsWithPVC" in protections["extraEnabled"]
    protected_classes = {entry["name"] for entry in protections["config"]["PodsWithPVC"]["protectedStorageClasses"]}
    # nodeFit does not account for a strict-local PV, so eviction cannot move these Pods.
    local_classes = {
        storage_class["name"]
        for storage_class in values["storageClasses"]
        if storage_class["parameters"]["linstor.csi.linbit.com/allowRemoteVolumeAccess"] == "false"
    }
    assert local_classes
    assert local_classes <= protected_classes


def test_piraeus_controller_network_policy_only_admits_api_tls(cluster_chart: Chart) -> None:
    policy = next(
        obj for obj in cluster_chart.api_objects if obj.kind == "NetworkPolicy" and obj.name == "linstor-controller-api"
    ).to_json()

    assert policy["metadata"]["namespace"] == "piraeus-datastore"
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "linstor-controller"}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [{"ports": [{"port": 3371, "protocol": "TCP"}]}]


if __name__ == "__main__":
    pytest_bazel.main()
