"""Pinning tests for the cdk8s manifest generators.

The generated-output snapshot (STYLE.md § Testing) over every file the generator writes:
the committed files are the source of truth, and regeneration must reproduce them exactly.
`GENERATED_ROOT` is closed in the other direction too, so a hand-added or stale file there
fails; generated files beside hand-written ones under `HAND_WRITTEN_ROOT` or `PARKED_ROOT`
are pinned only one way.
"""

from difflib import unified_diff
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.cdk8s.generate_manifests import generate_manifests
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT, PARKED_ROOT
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

_REGENERATE = "regenerate with `bb run //cluster/cdk8s:generate_manifests` and commit the result"


def _files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory, checkout: Path) -> Path:
    root = tmp_path_factory.mktemp("generated")
    generate_manifests(root)
    # CI uses the same pinned generator as local runs. Preserve its exact drift as an
    # applyable artifact rather than making authors reconstruct generated YAML by hand.
    patch: list[str] = []
    for relative in sorted(_files(root)):
        committed = checkout / relative
        before = committed.read_text().splitlines(keepends=True) if committed.is_file() else []
        after = (root / relative).read_text().splitlines(keepends=True)
        patch.extend(
            unified_diff(
                before, after, fromfile=f"a/{relative}" if committed.is_file() else "/dev/null", tofile=f"b/{relative}"
            )
        )
    if patch:
        (undeclared_outputs_dir() / "generated-manifests.patch").write_text("".join(patch))
    return root


@pytest.fixture(scope="module")
def parsed_yaml_documents(generated: Path) -> dict[Path, tuple[Any, ...]]:
    """Cache parsed documents for read-only inspection by manifest assertions."""
    return {path: tuple(yaml.load_all(path.read_text(), Loader=yaml.CSafeLoader)) for path in generated.rglob("*.yaml")}


def _generated_documents(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> list[dict[str, Any]]:
    """Objects written by cdk8s, including generated files beside hand-written inputs."""
    generated_root = generated / GENERATED_ROOT
    hand_written_root = generated / HAND_WRITTEN_ROOT
    return [
        doc
        for path, documents in parsed_yaml_documents.items()
        if path.is_relative_to(generated_root)
        or (path.is_relative_to(hand_written_root) and path.name.endswith(".k8s.yaml"))
        for doc in documents
        if isinstance(doc, dict)
    ]


def test_kvm_worker_pool_members_are_selected_fenced_and_given_their_own_ssd_disk(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    pool = next(doc for doc in documents if doc["kind"] == "VirtualMachinePool")
    template = pool["spec"]["virtualMachineTemplate"]
    labels = pool["spec"]["selector"]["matchLabels"]
    # A template change must not replace every worker at once, which would leave the pool without an executor.
    assert pool["spec"]["maxUnavailable"] == 1
    assert labels.items() <= template["metadata"]["labels"].items()
    assert labels.items() <= template["spec"]["template"]["metadata"]["labels"].items()
    guest = template["spec"]["template"]["spec"]
    assert guest["nodeSelector"] == {"topology.kubernetes.io/region": "hil"}
    # Every pool member avoids any node that already runs a worker.
    (term,) = guest["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert term["topologyKey"] == "kubernetes.io/hostname"
    assert {
        (expression["key"], value)
        for expression in term["labelSelector"]["matchExpressions"]
        for value in expression["values"]
    } == labels.items()
    # A member is ready only once its executor has registered; the readiness endpoint is the executor's own.
    probe = guest["readinessProbe"]["httpGet"]
    assert probe["path"] == "/readyz"
    assert {"name": "server-type", "value": "buildbuddy-executor"} in probe["httpHeaders"]
    (work,) = template["spec"]["dataVolumeTemplates"]
    storage = work["spec"]["storage"]
    assert storage["storageClassName"] == "piraeus-ssd-local"
    assert storage["volumeMode"] == "Filesystem"
    (disk_volume,) = [volume for volume in guest["volumes"] if volume["name"] == "work"]
    assert disk_volume["dataVolume"]["name"] == work["metadata"]["name"]
    # The egress fence selects pool members by the shared worker label.
    fence = next(
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy"
        and doc["metadata"]["name"].startswith("buildbuddy-kvm-worker")
        and doc["spec"]["endpointSelector"]["matchLabels"] == labels
    )["spec"]
    assert fence["egress"], fence
    assert all(not rule.get("toEntities") and not rule.get("toCIDR") for rule in fence["egress"])


def test_kvm_worker_metrics_are_scrapable_only_by_node_local_alloy(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    pool = next(doc for doc in documents if doc["kind"] == "VirtualMachinePool")
    service = next(
        doc
        for doc in documents
        if doc["kind"] == "Service" and doc["metadata"]["name"] == "buildbuddy-kvm-worker-metrics"
    )
    monitor = next(
        doc
        for doc in documents
        if doc["kind"] == "ServiceMonitor" and doc["metadata"]["name"] == "buildbuddy-kvm-worker"
    )
    ports = {port["name"]: port for port in service["spec"]["ports"]}
    pod_labels = pool["spec"]["virtualMachineTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert service["spec"]["selector"].items() <= pod_labels.items()
    assert monitor["spec"]["selector"]["matchLabels"].items() <= service["metadata"]["labels"].items()
    assert {endpoint["port"] for endpoint in monitor["spec"]["endpoints"]} == ports.keys()
    for policy in (
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy" and doc["metadata"]["name"].startswith("buildbuddy-kvm-worker")
    ):
        (rule,) = policy["spec"]["ingress"]
        assert rule["fromEndpoints"] == [
            {"matchLabels": {"app.kubernetes.io/name": "alloy-node", "k8s:io.kubernetes.pod.namespace": "monitoring"}}
        ]
        assert {p["port"] for p in rule["toPorts"][0]["ports"]} == {str(port["port"]) for port in ports.values()}


def test_cpap_guest_filesystem_metrics_are_scraped_only_by_alloy(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    vm = next(doc for doc in documents if doc["kind"] == "VirtualMachine" and doc["metadata"]["name"] == "cpap-gateway")
    vm_template = vm["spec"]["template"]
    interface = vm_template["spec"]["domain"]["devices"]["interfaces"][0]
    assert {port["name"]: port["port"] for port in interface["ports"]} == {"http": 18080, "node-metrics": 9100}

    service = next(
        doc for doc in documents if doc["kind"] == "Service" and doc["metadata"]["name"] == "cpap-gateway-metrics"
    )
    monitor = next(
        doc
        for doc in documents
        if doc["kind"] == "ServiceMonitor" and doc["metadata"]["name"] == "cpap-gateway-metrics"
    )
    assert service["spec"]["selector"].items() <= vm_template["metadata"]["labels"].items()
    assert monitor["spec"]["selector"]["matchLabels"].items() <= service["metadata"]["labels"].items()
    assert monitor["spec"]["endpoints"] == [{"port": "node-metrics", "path": "/metrics"}]

    policy = next(
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy" and doc["metadata"]["name"] == "guest-metrics-ingress"
    )["spec"]
    assert policy["endpointSelector"]["matchLabels"].items() <= vm_template["metadata"]["labels"].items()
    assert policy["ingress"] == [
        {
            "fromEndpoints": [
                {
                    "matchLabels": {
                        "app.kubernetes.io/component": "sync",
                        "app.kubernetes.io/name": "cpap-sync",
                        "k8s:io.kubernetes.pod.namespace": "cpap-sync",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "18080", "protocol": "TCP"}]}],
        },
        {
            "fromEndpoints": [
                {
                    "matchLabels": {
                        "app.kubernetes.io/name": "alloy-node",
                        "k8s:io.kubernetes.pod.namespace": "monitoring",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "9100", "protocol": "TCP"}]}],
        },
    ]
    assert "egress" not in policy


def test_kvm_worker_keeps_privileged_actions_inside_the_guest(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    pool = next(doc for doc in documents if doc["kind"] == "VirtualMachinePool")
    worker = pool["spec"]["virtualMachineTemplate"]["spec"]
    guest = worker["template"]["spec"]
    assert pool["spec"]["maxUnavailable"] == 1
    assert worker["runStrategy"] == "Always"
    assert guest["nodeSelector"] == {"topology.kubernetes.io/region": "hil"}
    kubevirt = next(doc["spec"] for doc in documents if doc["kind"] == "KubeVirt")
    control_plane_toleration = {
        "key": "node-role.kubernetes.io/control-plane",
        "operator": "Exists",
        "effect": "NoSchedule",
    }
    assert control_plane_toleration in guest["tolerations"]
    assert control_plane_toleration in kubevirt["infra"]["nodePlacement"]["tolerations"]
    assert "kubernetes.io/hostname" not in kubevirt["infra"]["nodePlacement"].get("nodeSelector", {})
    workload_placement = kubevirt["workloads"]["nodePlacement"]
    assert control_plane_toleration in workload_placement["tolerations"]
    assert not workload_placement.get("nodeSelector")
    assert not workload_placement.get("affinity")
    assert guest["domain"]["cpu"]["model"] == "host-passthrough"
    cpu_features = {
        feature["name"] for feature in guest["domain"]["cpu"]["features"] if feature["policy"] == "optional"
    }
    terms = guest["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert all(len(term["matchExpressions"]) == 1 for term in terms)
    allowed_features = {
        expression["key"].removeprefix("cpu-feature.node.kubevirt.io/")
        for term in terms
        for expression in term["matchExpressions"]
        if expression["operator"] == "In" and expression["values"] == ["true"]
    }
    assert cpu_features == allowed_features == {"svm", "vmx"}
    assert guest["domain"]["resources"]["limits"] == guest["domain"]["resources"]["requests"]
    assert not guest["domain"]["devices"].get("hostDevices")
    assert not guest["domain"]["devices"].get("gpus")
    assert not any("hostDisk" in volume or "serviceAccount" in volume for volume in guest["volumes"])
    # These serials are the guest's cloud-init mount/credential contract.
    disks = {disk.get("serial"): disk["name"] for disk in guest["domain"]["devices"]["disks"]}
    volumes = {volume["name"]: volume for volume in guest["volumes"]}
    assert volumes[disks["bbkey"]]["secret"]["secretName"] == "buildbuddy-api-key"
    assert "dataVolume" in volumes[disks["work"]]
    user_data = next(
        volume["cloudInitNoCloud"]["userData"] for volume in guest["volumes"] if "cloudInitNoCloud" in volume
    )
    assert len(user_data.encode()) <= 2048  # KubeVirt's inline cloud-init admission limit.
    cloud_init = yaml.safe_load(user_data)
    (journald_dropin,) = [
        file
        for file in cloud_init["write_files"]
        if file["path"] == "/etc/systemd/journald.conf.d/90-buildbuddy-kvm-worker.conf"
    ]
    assert journald_dropin["permissions"] == "0644"
    assert "ForwardToConsole=yes" in journald_dropin["content"]
    assert "TTYPath=/dev/ttyS0" in journald_dropin["content"]
    assert "MaxLevelConsole=info" in journald_dropin["content"]
    assert "SystemMaxUse=32M" in journald_dropin["content"]
    assert "SystemKeepFree=192M" in journald_dropin["content"]
    assert "RuntimeMaxUse=16M" in journald_dropin["content"]
    assert cloud_init["runcmd"][0] == ["systemctl", "restart", "systemd-journald.service"]
    (exporter_install,) = [command for command in cloud_init["runcmd"] if "prometheus-node-exporter" in command]
    assert "--no-install-recommends" in exporter_install
    mounted = {mount[0]: mount[1] for mount in cloud_init["mounts"]}
    assert mounted["/dev/disk/by-id/virtio-bbkey"] == "/run/buildbuddy-key"
    assert mounted["/dev/disk/by-id/virtio-work"] == "/buildbuddy"
    config_map = next(
        doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "buildbuddy-kvm-worker"
    )
    assert volumes[disks["bbconfig"]]["configMap"]["name"] == config_map["metadata"]["name"]
    policy = next(
        doc
        for doc in documents
        if doc["kind"] == "CiliumNetworkPolicy" and doc["metadata"]["name"] == "buildbuddy-kvm-worker"
    )["spec"]
    assert policy["endpointSelector"]["matchLabels"].items() <= worker["template"]["metadata"]["labels"].items()
    # Ingress admits only named peers (the metrics scraper); the exact peer and port are pinned in
    # test_kvm_worker_metrics_are_scrapable_only_by_node_local_alloy.
    assert all(
        rule.get("fromEndpoints") and not rule.get("fromEntities") and not rule.get("fromCIDR")
        for rule in policy["ingress"]
    )
    assert all(not rule.get("toEntities") and not rule.get("toCIDR") for rule in policy["egress"])


@pytest.fixture(scope="module")
def checkout(runfiles: Runfiles) -> Path:
    """The runfiles tree, which holds each committed file the data deps package at its repo path."""
    return Path(str(runfiles.root() / "ducktape/MODULE.bazel")).parent


def test_generated_tree_matches_committed_snapshot(generated: Path, checkout: Path) -> None:
    generated_files = _files(generated)
    committed_files = _files(checkout / GENERATED_ROOT)
    missing = sorted(relative for relative in generated_files if not (checkout / relative).is_file())
    stale = sorted(
        relative
        for relative in generated_files
        if (checkout / relative).is_file() and (generated / relative).read_text() != (checkout / relative).read_text()
    )
    committed = {f"{GENERATED_ROOT}/{relative}" for relative in committed_files}
    extra = sorted(committed - generated_files)

    failures = []
    if missing:
        failures.append(f"Generated but not committed ({_REGENERATE}):\n" + "\n".join(missing))
    if stale:
        failures.append(f"Stale ({_REGENERATE}):\n" + "\n".join(stale))
    if extra:
        failures.append(
            f"Committed under {GENERATED_ROOT} but not written by the generator; delete it, or keep a "
            "directory holding a hand-written file whole under the hand-written root:\n" + "\n".join(extra)
        )

    assert not failures, "\n\n".join(failures)


def test_piraeus_storage_is_confined_to_prepared_nodes(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    release = next(
        doc
        for doc in documents
        if doc.get("kind") == "HelmRelease" and doc["metadata"].get("name") == "linstor-cluster"
    )
    values = release["spec"]["values"]
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
    assert 'address = "127.0.0.1:9942"' in reactor_patch["patch"]
    assert "satellite-config" not in reactor_patch["patch"]
    assert "bind_address" not in reactor_patch["patch"]
    assert "3366" not in reactor_patch["patch"]

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
    # Consumers name these four StorageClasses directly, so their names and pool types
    # are a deployment contract. Every class must resolve to a pool on a prepared node.
    expected_class_pools = {
        "piraeus-ssd-local": "ssd",
        "piraeus-hdd-local": "hdd",
        "piraeus-ssd-2-autodiskful": "ssd",
        "piraeus-hdd-2": "hdd",
    }
    assert set(expected_class_pools) <= set(classes)
    available_pools = {name for pools in actual_devices.values() for name in pools}
    local_classes = {"piraeus-ssd-local", "piraeus-hdd-local"}
    replicated_classes = {"piraeus-ssd-2-autodiskful", "piraeus-hdd-2"}
    for name, storage_class in classes.items():
        assert storage_class["reclaimPolicy"] == "Retain"
        assert storage_class["volumeBindingMode"] == "WaitForFirstConsumer"
        assert storage_class["allowVolumeExpansion"] is True
        parameters = storage_class["parameters"]
        assert parameters["linstor.csi.linbit.com/storagePool"] in available_pools
        if name in expected_class_pools:
            assert parameters["linstor.csi.linbit.com/storagePool"] == expected_class_pools[name]
        if name in local_classes:
            assert parameters["linstor.csi.linbit.com/placementCount"] == "1"
            assert parameters["linstor.csi.linbit.com/allowRemoteVolumeAccess"] == "false"
        elif name in replicated_classes:
            # Replicated copies and attachments stay inside OVH, never across the home link.
            assert parameters["linstor.csi.linbit.com/placementCount"] == "2"
            assert parameters["linstor.csi.linbit.com/replicasOnSame"] == "topology.kubernetes.io/zone=hil-ovh"
            assert yaml.safe_load(parameters["linstor.csi.linbit.com/allowRemoteVolumeAccess"]) == [
                {"fromSame": ["topology.kubernetes.io/zone"]}
            ]
            assert parameters["DrbdOptions/auto-quorum"] == "suspend-io"
            assert parameters["DrbdOptions/auto-diskful"] == "1"
            assert parameters["DrbdOptions/auto-diskful-allow-cleanup"] == "true"

    operator_flux = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "piraeus-operator"
    )
    cluster_flux = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "piraeus-cluster"
    )
    assert operator_flux["spec"]["prune"] is False
    assert operator_flux["spec"]["deletionPolicy"] == "Orphan"
    assert cluster_flux["spec"]["prune"] is False
    assert cluster_flux["spec"]["deletionPolicy"] == "Orphan"
    assert cluster_flux["spec"]["dependsOn"] == [
        {"name": "piraeus-operator", "namespace": "ducktape-flux"},
        {"name": "cert-manager", "namespace": "ducktape-flux"},
        {"name": "monitoring-crds", "namespace": "ducktape-flux"},
    ]


def test_crossplane_core_waits_for_its_controller_and_retains_its_inventory(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    release = next(
        doc for doc in documents if doc.get("kind") == "HelmRelease" and doc["metadata"].get("name") == "crossplane"
    )
    kustomization = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "crossplane"
    )

    assert release["metadata"]["namespace"] == "crossplane-system"
    assert kustomization["spec"]["dependsOn"] == [{"name": "kyverno", "namespace": "ducktape-flux"}]
    assert kustomization["spec"]["wait"] is True
    assert kustomization["spec"]["prune"] is False


def test_litellm_provider_waits_for_crossplane_health_before_exposing_its_resources(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    provider = next(
        doc for doc in documents if doc.get("kind") == "Provider" and doc["metadata"].get("name") == "provider-litellm"
    )
    pull_secret = next(
        doc
        for doc in documents
        if doc.get("kind") == "ExternalSecret"
        and doc["metadata"].get("name") == "forgejo-images-creds"
        and doc["metadata"].get("namespace") == "crossplane-system"
    )
    kustomization = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "crossplane-provider-litellm"
    )

    assert provider["apiVersion"] == "pkg.crossplane.io/v1"
    assert provider["spec"]["package"].startswith("git.allegedly.works/ducktape-ci/provider-litellm:")
    assert provider["spec"]["packagePullSecrets"] == [{"name": "forgejo-images-creds"}]
    assert pull_secret["spec"]["secretStoreRef"] == {
        "kind": "ClusterSecretStore",
        "name": "kubernetes-crossplane-forgejo-images-creds-secret-store",
    }
    assert pull_secret["spec"]["target"]["template"]["type"] == "kubernetes.io/dockerconfigjson"
    store = next(
        doc
        for doc in documents
        if doc.get("kind") == "ClusterSecretStore"
        and doc["metadata"].get("name") == "kubernetes-crossplane-forgejo-images-creds-secret-store"
    )
    assert store["spec"]["conditions"] == [{"namespaces": ["crossplane-system"]}]
    assert store["spec"]["provider"]["kubernetes"]["remoteNamespace"] == "forgejo-images"
    assert store["spec"]["provider"]["kubernetes"]["auth"]["serviceAccount"] == {
        "name": "crossplane-forgejo-images-creds-reader"
    }
    reader_role = next(
        doc
        for doc in documents
        if doc.get("kind") == "Role" and doc["metadata"].get("name") == "crossplane-forgejo-images-creds-reader"
    )
    assert reader_role["metadata"]["namespace"] == "forgejo-images"
    assert reader_role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["forgejo-images-creds"], "verbs": ["get"]}
    ]
    reader_binding = next(
        doc
        for doc in documents
        if doc.get("kind") == "RoleBinding" and doc["metadata"].get("name") == "crossplane-forgejo-images-creds-reader"
    )
    assert reader_binding["subjects"] == [
        {
            "apiGroup": "",
            "kind": "ServiceAccount",
            "name": "crossplane-forgejo-images-creds-reader",
            "namespace": "crossplane-system",
        }
    ]
    assert kustomization["spec"]["dependsOn"] == [
        {"name": "crossplane", "namespace": "ducktape-flux"},
        {"name": "external-secrets-operator", "namespace": "ducktape-flux"},
        {"name": "forgejo-images", "namespace": "ducktape-flux"},
    ]
    assert kustomization["spec"]["wait"] is True
    assert kustomization["spec"]["prune"] is False
    assert kustomization["spec"]["healthCheckExprs"] == [
        {
            "apiVersion": "external-secrets.io/v1",
            "kind": "ExternalSecret",
            "current": "has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')",
            "failed": "has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'False')",
        },
        {
            "apiVersion": "pkg.crossplane.io/v1",
            "kind": "Provider",
            "current": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
            "failed": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
        },
    ]


def test_opentofu_workspace_takes_over_forgejo_root_after_tofu_and_preserves_state(
    runfiles: Runfiles, generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    provider = next(
        doc for doc in documents if doc.get("kind") == "Provider" and doc["metadata"].get("name") == "provider-opentofu"
    )
    provider_kustomization = next(
        doc
        for doc in documents
        if doc.get("kind") == "Kustomization" and doc.get("metadata", {}).get("name") == "crossplane-provider-opentofu"
    )
    provider_config = next(
        doc
        for doc in documents
        if doc.get("apiVersion") == "opentofu.upbound.io/v1beta1"
        and doc.get("kind") == "ProviderConfig"
        and doc.get("metadata", {}).get("name") == "crossplane-forgejo-agentydragon"
    )
    workspace = next(
        doc
        for doc in documents
        if doc.get("apiVersion") == "opentofu.upbound.io/v1beta1"
        and doc.get("kind") == "Workspace"
        and doc.get("metadata", {}).get("name") == "forgejo-agentydragon"
    )
    resources_kustomization = next(
        doc
        for doc in documents
        if doc.get("kind", "").lower() == "kustomization"
        and doc.get("metadata", {}).get("name") == "crossplane-forgejo-agentydragon"
    )
    assert provider["spec"]["package"].startswith("xpkg.upbound.io/upbound/provider-opentofu:")
    assert provider_kustomization["spec"]["dependsOn"] == [{"name": "crossplane", "namespace": "ducktape-flux"}]
    assert provider_kustomization["spec"]["wait"] is True
    assert provider_kustomization["spec"]["prune"] is False
    assert provider_kustomization["spec"]["healthCheckExprs"] == [
        {
            "apiVersion": "pkg.crossplane.io/v1",
            "kind": "Provider",
            "current": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'True')",
            "failed": "has(status.conditions) && status.conditions.exists(c, c.type == 'Healthy' && c.status == 'False')",
        }
    ]
    assert provider_config["spec"]["configuration"] == 'terraform {\n  backend "pg" {}\n}\n'
    assert 'schema_name = "forgejo_agentydragon"' in provider_config["spec"]["backendFile"]
    assert "PGPASSWORD" not in provider_config["spec"]["backendFile"]
    assert workspace["metadata"]["annotations"]["crossplane.io/external-name"] == "default"
    assert workspace["spec"]["providerConfigRef"] == {"name": "crossplane-forgejo-agentydragon"}
    assert workspace["spec"]["managementPolicies"] == ["Observe", "Create", "Update"]
    assert "Delete" not in workspace["spec"]["managementPolicies"]
    assert workspace["spec"]["deletionPolicy"] == "Orphan"
    assert workspace["spec"]["forProvider"]["source"] == "Inline"
    assert "remotePullPolicy" not in workspace["spec"]["forProvider"]
    root = runfiles.root() / "ducktape/tf/gitops/forgejo-agentydragon"
    assert workspace["spec"]["forProvider"]["module"] == "\n".join(
        source.read_text() for source in sorted(root.glob("*.tf"))
    )
    assert workspace["spec"]["forProvider"]["env"] == [
        {
            "name": "PGPASSWORD",
            "secretKeyRef": {"name": "tofu-state-db-credentials", "namespace": "flux-system", "key": "password"},
        }
    ]
    assert resources_kustomization["spec"]["dependsOn"] == [
        {"name": "crossplane-provider-opentofu", "namespace": "ducktape-flux"},
        {"name": "tofu-state-db", "namespace": "ducktape-flux"},
    ]
    assert resources_kustomization["spec"]["wait"] is True
    assert resources_kustomization["spec"]["prune"] is True
    assert not any(
        doc.get("apiVersion") == "infra.contrib.fluxcd.io/v1alpha2"
        and doc.get("kind") == "Terraform"
        and doc.get("metadata", {}).get("name") == "forgejo-agentydragon"
        for doc in documents
    )


def test_litellm_crossplane_keys_retain_publication_after_handoff(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    kustomizations = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "Kustomization"
        and doc.get("metadata", {}).get("name")
        in {"litellm-keys-tf", "litellm-crossplane-keys", "litellm-buildbuddy-sync"}
    }

    assert kustomizations["litellm-keys-tf"]["spec"]["dependsOn"] == [
        {"name": "tofu-controller", "namespace": "ducktape-flux"},
        {"name": "litellm", "namespace": "ducktape-flux"},
    ]
    assert kustomizations["litellm-crossplane-keys"]["spec"]["dependsOn"] == [
        {"name": "crossplane-provider-litellm", "namespace": "ducktape-flux"},
        {"name": "litellm", "namespace": "ducktape-flux"},
    ]
    assert kustomizations["litellm-crossplane-keys"]["spec"]["wait"] is True
    assert kustomizations["litellm-buildbuddy-sync"]["spec"]["dependsOn"] == [
        {"name": "tofu-controller", "namespace": "ducktape-flux"},
        {"name": "litellm-crossplane-keys", "namespace": "ducktape-flux"},
    ]

    keys = {
        doc["spec"]["forProvider"]["keyAlias"]: doc
        for doc in documents
        if doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Key"
    }
    assert keys
    assert len(keys) == sum(
        doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Key" for doc in documents
    )
    for alias in ("claude-subscription-clients", "codex-clients", "gemini-clients", "antigravity-clients"):
        assert keys[alias]["spec"]["forProvider"]["keyWoSecretRef"] == {
            "name": f"litellm-client-key-{alias}",
            "key": "api-key",
        }

    published_key_secrets = [
        doc
        for doc in documents
        if doc.get("kind") == "ExternalSecret"
        and any(
            data.get("remoteRef", {}).get("key", "").startswith("litellm-crossplane-key-")
            for data in doc.get("spec", {}).get("data", [])
        )
    ]
    assert published_key_secrets
    published_connections = [
        data["remoteRef"]["key"]
        for doc in published_key_secrets
        for data in doc["spec"]["data"]
        if data["remoteRef"]["key"].startswith("litellm-crossplane-key-")
    ]
    assert len(published_connections) == len(set(published_connections))
    assert set(published_connections) == {
        key["spec"]["writeConnectionSecretToRef"]["name"]
        for key in keys.values()
        if "writeConnectionSecretToRef" in key["spec"]
    }
    assert all(
        data["remoteRef"]["property"] == "attribute.key"
        for doc in published_key_secrets
        for data in doc["spec"]["data"]
        if data["remoteRef"]["key"].startswith("litellm-crossplane-key-")
    )

    teams = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("apiVersion") == "litellm.m.crossplane.io/v1alpha1" and doc.get("kind") == "Team"
    }
    assert "routerSettings" not in teams["team-antigravity-clients"]["spec"]["forProvider"]
    assert teams["team-codex-clients"]["spec"]["forProvider"]["routerSettings"]["fallbacks"]
    assert teams["team-gemini-clients"]["spec"]["forProvider"]["routerSettings"]["fallbacks"]


def test_litellm_buildbuddy_terraform_runners_receive_api_key_inputs(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    terraform_resources = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "Terraform"
        and doc.get("metadata", {}).get("name") in {"litellm-keys", "litellm-buildbuddy-sync"}
    }
    assert set(terraform_resources) == {"litellm-buildbuddy-sync"}

    expected_env = {
        "name": "BUILDBUDDY_API_KEY",
        "valueFrom": {"secretKeyRef": {"name": "buildbuddy-org-admin-api-key", "key": "api-key"}},
    }
    expected_var = {
        "name": "buildbuddy_api_key",
        "valueFrom": {"secretKeyRef": {"name": "buildbuddy-org-admin-api-key", "key": "api-key"}},
    }
    for resource in terraform_resources.values():
        env = resource["spec"]["runnerPodTemplate"]["spec"]["env"]
        assert expected_env in env
        assert expected_var in resource["spec"]["vars"]
        assert not any(variable["name"].startswith("TF_VAR_") for variable in env)


def test_piraeus_controller_network_policy_only_admits_api_tls(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    policy = next(
        document
        for document in _generated_documents(generated, parsed_yaml_documents)
        if document.get("kind") == "NetworkPolicy" and document["metadata"].get("name") == "linstor-controller-api"
    )

    assert policy["metadata"]["namespace"] == "piraeus-datastore"
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/component": "linstor-controller"}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [{"ports": [{"port": 3371, "protocol": "TCP"}]}]


def test_ducktape_artifact_copy_sources_are_in_sparse_checkout(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    documents = [
        document
        for path in generated.rglob("*.yaml")
        for document in parsed_yaml_documents[path]
        if isinstance(document, dict)
    ]
    source = next(
        document
        for document in documents
        if document["kind"] == "GitRepository"
        and document["metadata"].get("name") == "ducktape"
        and document["metadata"].get("namespace") == "ducktape-flux"
    )
    sparse_roots = tuple(path.strip("/") for path in source["spec"].get("sparseCheckout", []))
    generator = next(
        document
        for document in documents
        if document["kind"] == "ArtifactGenerator"
        and document["metadata"].get("name") == "ducktape-artifacts"
        and document["metadata"].get("namespace") == "ducktape-flux"
    )
    repo_aliases = {
        artifact_source["alias"]
        for artifact_source in generator["spec"]["sources"]
        if artifact_source["kind"] == "GitRepository"
        and artifact_source["name"] == "ducktape"
        and artifact_source.get("namespace", "ducktape-flux") == "ducktape-flux"
    }
    assert repo_aliases, "ducktape-artifacts must read the ducktape GitRepository"

    missing: list[str] = []
    for artifact in generator["spec"]["artifacts"]:
        for copy in artifact["copy"]:
            alias, separator, pattern = copy["from"].removeprefix("@").partition("/")
            if alias not in repo_aliases or not separator:
                continue
            literal_parts = []
            for part in pattern.split("/"):
                if any(character in part for character in "*?[{"):
                    break
                literal_parts.append(part)
            required_root = "/".join(literal_parts)
            if not any(required_root == root or required_root.startswith(f"{root}/") for root in sparse_roots):
                missing.append(f"{artifact['name']}: {copy['from']}")

    assert not missing, (
        "ducktape ArtifactGenerator copy sources must be covered by the ducktape GitRepository sparseCheckout:\n"
        + "\n".join(missing)
    )


def test_session_archive_credentials_source_reaches_its_secret_generator(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]], checkout: Path
) -> None:
    credentials_source = "secrets/shared/session-backup.yaml"
    assert (checkout / credentials_source).is_file(), "the encrypted credentials source must be in the source checkout"

    documents = _generated_documents(generated, parsed_yaml_documents)
    generator = next(
        document
        for document in documents
        if document.get("kind") == "ArtifactGenerator" and document["metadata"].get("name") == "ducktape-artifacts"
    )
    archive_artifact = next(item for item in generator["spec"]["artifacts"] if item["name"] == "ai-session-archive")
    copies = archive_artifact["copy"]
    assert {(copy["from"], copy["to"]) for copy in copies} >= {
        ("@repo/cluster/generated/ai-session-archive/**", "@artifact/cluster/generated/ai-session-archive/"),
        (
            "@repo/secrets/shared/session-backup.yaml",
            "@artifact/cluster/generated/ai-session-archive/session-backup.sops.yaml",
        ),
    }

    kustomization = yaml.safe_load((generated / "cluster/generated/ai-session-archive/kustomization.yaml").read_text())
    assert kustomization["secretGenerator"] == [
        {
            "name": "session-backup-importer-credentials",
            "namespace": "session-backup",
            "files": ["session-backup.yaml=session-backup.sops.yaml"],
        }
    ]


def test_no_image_automation_markers(generated: Path) -> None:
    # cdk8s can't emit YAML comments, so this should be unreachable -- but if it ever did,
    # Flux's image-automation bot would silently fight the generator for ownership of the
    # file (cluster/docs/cdk8s.md).
    marked = sorted(relative for relative in _files(generated) if "$imagepolicy" in (generated / relative).read_text())
    assert not marked, "Generated files must not carry a Flux image-automation marker:\n" + "\n".join(marked)


def test_flux_kustomizations_under_parked_path_are_annotated(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    flux_chart = generated / HAND_WRITTEN_ROOT / "flux/kustomizations.k8s.yaml"
    missing = sorted(
        doc["metadata"]["name"]
        for doc in parsed_yaml_documents[flux_chart]
        if isinstance(doc, dict)
        and doc.get("kind") == "Kustomization"
        and Path(doc["spec"]["path"].removeprefix("./")).is_relative_to(PARKED_ROOT)
        and doc["metadata"].get("annotations", {}).get("ducktape.org/parked") != "true"
    )
    assert not missing, f"Kustomizations under {PARKED_ROOT}/ lack the parked annotation: {missing}"


def test_goldilocks_explicit_decision(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
    update_mode = "goldilocks.fairwinds.com/vpa-update-mode"
    enabled = "goldilocks.fairwinds.com/enabled"
    missing = sorted(
        f"{path.relative_to(generated)}: Namespace '{doc['metadata']['name']}'"
        for path, documents in parsed_yaml_documents.items()
        if path.is_relative_to(generated / GENERATED_ROOT)
        or (path.is_relative_to(generated / HAND_WRITTEN_ROOT) and path.name.endswith(".k8s.yaml"))
        for doc in documents
        if isinstance(doc, dict)
        and doc.get("kind") == "Namespace"
        and update_mode not in (labels := (doc["metadata"].get("labels") or {}))
        and labels.get(enabled) != "false"
    )
    assert not missing, "Generated Namespaces must label their Goldilocks decision:\n" + "\n".join(missing)


def test_policy_bindings_resolve_sets(generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]) -> None:
    documents = _generated_documents(generated, parsed_yaml_documents)
    policy_sets = {
        (doc["metadata"].get("namespace", ""), doc["metadata"]["name"])
        for doc in documents
        if doc.get("kind") == "PolicySet"
    }
    missing = [
        f"PolicyBinding '{namespace}/{doc['metadata']['name']}' names PolicySet '{policy_set}', which is not rendered in {namespace}"
        for doc in documents
        if doc.get("kind") == "PolicyBinding"
        for namespace in [doc["metadata"].get("namespace", "")]
        for policy_set in doc["spec"].get("policySets", [])
        if (namespace, policy_set) not in policy_sets
    ]
    assert not missing, "\n".join(missing)


def test_cluster_diagnostics_kustomizations_are_read_only(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    role = next(
        doc
        for doc in _generated_documents(generated, parsed_yaml_documents)
        if doc.get("kind") == "ClusterRole" and doc["metadata"]["name"] == "cluster-diagnostics-reader"
    )
    assert "aggregationRule" not in role

    def grants(group: str, resource: str, verb: str) -> bool:
        return any(
            not rule.get("resourceNames")
            and (group in rule.get("apiGroups", []) or "*" in rule.get("apiGroups", []))
            and (resource in rule.get("resources", []) or "*" in rule.get("resources", []))
            and (verb in rule.get("verbs", []) or "*" in rule.get("verbs", []))
            for rule in role["rules"]
        )

    reads = ("get", "list", "watch")
    writes = ("create", "update", "patch", "delete", "deletecollection")
    assert all(grants("kustomize.toolkit.fluxcd.io", "kustomizations", verb) for verb in reads)
    assert not any(grants("kustomize.toolkit.fluxcd.io", "kustomizations", verb) for verb in writes)


def test_parked_haku_backup_and_shared_services_remain_configured(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    active = [
        doc
        for path in generated.rglob("*.k8s.yaml")
        if "parked" not in path.parts
        for doc in parsed_yaml_documents[path]
        if doc
    ]
    owners = {doc["metadata"]["name"]: doc for doc in active if doc["kind"] == "Kustomization"}
    # Keep Haku's namespace and the public coder's namespace and access Roles alive.
    assert {"haku-namespace", "public-coder-agent-access"} <= owners.keys()
    assert any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == "public-coder-agent" for doc in active)
    # Only the retained backup recovery package remains reproducible outside the live roots.
    archived = generated / "cluster/parked/haku-openclaw-spike"
    backup = parsed_yaml_documents[archived / "backup/backup.k8s.yaml"]
    bucket = next(doc for doc in backup if doc["kind"] == "Bucket")
    assert bucket["spec"]["reclaimPolicy"] == "Retain"
    credentials = next(doc for doc in backup if doc["kind"] == "S3Credentials")
    assert credentials["spec"]["reclaimPolicy"] == "Retain"
    assert (archived / "backup/repository.sops.yaml").is_file()
    assert (archived / "backup/README.md").is_file()


def test_parked_claude_namespace_preserves_credentials_and_egress_fence(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    active = [
        doc
        for path in generated.rglob("*.k8s.yaml")
        if "parked" not in path.parts
        for doc in parsed_yaml_documents[path]
        if isinstance(doc, dict)
    ]
    # Claude's shared roles, identities and credentials survive; ad-hoc compute does not.
    claude = [doc for doc in active if doc["metadata"].get("namespace") == "claude-sandbox"]
    owners = {doc["metadata"]["name"]: doc for doc in active if doc["kind"] == "Kustomization"}
    quota = next(doc for doc in claude if doc["kind"] == "ResourceQuota")
    assert str(quota["spec"]["hard"]["pods"]) == "0"
    fence = next(
        doc for doc in claude if doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "parked-compute-egress"
    )
    assert fence["spec"] == {"podSelector": {}, "policyTypes": ["Egress"], "egress": []}
    assert {"buildbuddy-api-key", "claude-forgejo-credentials"} <= {
        doc["metadata"]["name"] for doc in claude if doc["kind"] == "ExternalSecret"
    }
    assert not any(doc["kind"] == "ClusterPolicy" and doc["metadata"]["name"] == "inject-mitmproxy" for doc in active)
    assert {"agent-sandbox-controller", "public-coder-agent-access", "claude-sandbox-secrets"} <= owners.keys()


def test_openclaw_cutover_waits_for_agentplane_credentials(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    owners = {
        doc["metadata"]["name"]: doc
        for path in generated.rglob("*.k8s.yaml")
        for doc in parsed_yaml_documents[path]
        if isinstance(doc, dict) and doc["kind"] == "Kustomization"
    }
    app = owners["public-coder-agent-app"]["spec"]
    assert "agentplane-staging" in {dependency["name"] for dependency in app["dependsOn"]}
    assert app["deletionPolicy"] == "Orphan"
    checks = owners["agentplane-staging"]["spec"]["healthChecks"]
    assert {c["name"] for c in checks if c["kind"] == "ExternalSecret"} >= {
        "public-coder-clickhouse",
        "public-coder-matrix",
        "brave-search",
    }


def test_public_coder_pause_retains_state_after_devbox_retirement(
    generated: Path, parsed_yaml_documents: dict[Path, tuple[Any, ...]]
) -> None:
    roots = (generated / "cluster/parked/public-coder-agent", generated / "cluster/generated/agents/public-coder-agent")
    objects = [
        doc
        for path, docs in parsed_yaml_documents.items()
        if path.name.endswith(".k8s.yaml") and any(path.is_relative_to(root) for root in roots)
        for doc in docs
        if isinstance(doc, dict)
    ]
    deployments = [doc for doc in objects if doc["kind"] == "Deployment"]
    assert {doc["metadata"]["name"] for doc in deployments} == {"public-coder-agent"}
    assert all(doc["spec"]["replicas"] == 0 for doc in deployments)
    assert {doc["metadata"]["name"] for doc in objects if doc["kind"] == "PersistentVolumeClaim"} == {
        "public-coder-agent-state-v2",
        "public-coder-agent-diagnostics",
        "public-coder-agent-sshpiper-recordings",
    }
    assert any(doc["kind"] == "Namespace" and doc["metadata"]["name"] == "public-coder-agent" for doc in objects)
    # VolSync owns its cache PVC; keep the backup source rather than deleting its owner.
    assert any(doc["kind"] == "ReplicationSource" for doc in objects)
    owners = {
        doc["metadata"]["name"]: doc["spec"]
        for docs in parsed_yaml_documents.values()
        for doc in docs
        if isinstance(doc, dict) and doc.get("kind") == "Kustomization" and "spec" in doc
    }
    for name in ("public-coder-agent-access", "public-coder-agent-app", "public-coder-agent-backup"):
        assert owners[name]["deletionPolicy"] == "Orphan"
    assert owners["public-coder-agent-app"].get("suspend", False) is True
    assert owners["public-coder-agent-backup"]["suspend"] is True


if __name__ == "__main__":
    pytest_bazel.main()
