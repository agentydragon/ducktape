"""Piraeus Operator with local SSD and HDD pools."""

from __future__ import annotations

from typing import Any

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap, k8s
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from python.runfiles import Runfiles

from cluster.cdk8s import namespaces, node_scheduling, piraeus_metrics, piraeus_storage
from cluster.cdk8s.container_resources import quantities
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, oci_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import mimir
from cluster.cdk8s.namespaces import PRIVILEGED_POD_SECURITY, Vpa

NAMESPACE = "piraeus-datastore"
OPERATOR_OUTPUT_DIR = f"{GENERATED_ROOT}/piraeus/operator"
CLUSTER_OUTPUT_DIR = f"{GENERATED_ROOT}/piraeus/cluster"

_PIRAEUS_READY_LABEL = "storage.allegedly.works/piraeus-ready"
# Controller Pods can run on any prepared OVH node.
_CONTROLLER_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "topology.kubernetes.io/region": "hil"}
_CLUSTER_NODE_AFFINITY = {
    "nodeSelectorTerms": [{"matchExpressions": [{"key": _PIRAEUS_READY_LABEL, "operator": "In", "values": ["true"]}]}]
}
_VOLUME_GROUPS = {"ssd": "piraeus_ssd", "hdd": "piraeus_hdd"}
_THIN_POOL = "data"
_FIRST_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "ovh-ns1001419"}
_FIRST_NODE_DATA_DEVICE = "/dev/disk/by-id/nvme-WDC_CL_SN720_SDAQNTW-512G-2000_1951A3449505"
_HDD_FIRST_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "ovh-ns103656"}
_HDD_FIRST_NODE_DATA_DEVICE = "/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H8UMLA"
_HDD_NEXT_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "ovh-ns103711"}
_HDD_NEXT_NODE_DATA_DEVICE = "/dev/disk/by-id/ata-HGST_HUS726020ALA610_K5H48V6A"
_HDD_LAST_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "ovh-ns102453"}
_HDD_LAST_NODE_DATA_DEVICE = "/dev/disk/by-id/ata-HGST_HUS726T4TALA6L1_V3H370PN"
_NEXT_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "ovh-ns104952"}
_NEXT_NODE_DATA_DEVICE = "/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8304019P450RGN"
_THIRD_NODE_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "ovh-ns104963"}
_THIRD_NODE_DATA_DEVICE = "/dev/disk/by-id/nvme-INTEL_SSDPE2MX450G7_BTPF8256009U450RGN"
_OPTIPLEX_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "optiplex"}
_OPTIPLEX_DATA_DEVICE = "/dev/disk/by-partlabel/r-piraeus"
_WYRM2_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "wyrm2"}
# Atlas zvols hot-added with these Proxmox disk serials: rpool/data (single NVMe) and tank (raidz2).
_WYRM2_SSD_DATA_DEVICE = "/dev/disk/by-id/virtio-piraeus-ssd"
_WYRM2_HDD_DATA_DEVICE = "/dev/disk/by-id/virtio-piraeus-hdd"
_RUGGED_SELECTOR = {_PIRAEUS_READY_LABEL: "true", "kubernetes.io/hostname": "rugged"}
# The opened LUKS partition behind rugged's root (nix/nixos/hosts/rugged/default.nix crypttab).
_RUGGED_DATA_DEVICE = "/dev/mapper/piraeus-ssd"
_CONTROLLER_POD_SELECTOR = {"app.kubernetes.io/component": "linstor-controller"}
_RUNFILES_ROOT = Runfiles.CreateOrRaise().root()
_REACTOR_CONFIG_PATCH = (_RUNFILES_ROOT / "ducktape/cluster/cdk8s/piraeus-reactor-patch.yaml").read_text()
_DRBD_ALLOY_CONFIG = (_RUNFILES_ROOT / "ducktape/cluster/cdk8s/piraeus-drbd-metrics.alloy").read_text()
_DRBD_ALLOY_CONFIG_MAP = "piraeus-drbd-alloy"
_DRBD_ALLOY_IMAGE = "docker.io/grafana/alloy:v1.21.1"
_DRBD_ALLOY_LABELS = {"app.kubernetes.io/name": "piraeus-drbd-metrics"}
# Online discard hands blocks freed by deletes back to the overcommitted LVM-thin pools.
# The CSI driver copies mountOpts into each volume's context at creation, so existing PVs
# need spec.mountOptions instead. StorageClass parameters are immutable: changing this
# means deleting the StorageClasses before the HelmRelease upgrade.
_DISCARD_MOUNT_OPTS = {"linstor.csi.linbit.com/mountOpts": "discard"}
# Reactor is loopback-only while the chart's satellite PodMonitor targets pod IP. This
# release is scoped to piraeus-datastore; chart output omits metadata.namespace, so
# the post-render target selects this one named monitor in this release.
_SATELLITE_POD_MONITOR_DELETE_PATCH = """\
$patch: delete
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: linstor-satellite
"""
_HDD_PROPERTIES = [{"name": "ExtCmd/WaitTimeout", "value": "180000"}]
_SATELLITE_PATCHES = [
    {"target": {"version": "v1", "kind": "ConfigMap", "name": "reactor-config"}, "patch": _REACTOR_CONFIG_PATCH}
]
_TALOS_SATELLITE_POD_TEMPLATE = {
    "spec": {
        # DRBD uses the host network. Talos kubelet selects node InternalIPs from
        # 10.42.0.0/16, the Nebula mesh subnet.
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        # Talos supplies DRBD modules in its system extension. Apply Piraeus' documented
        # Talos patch to remove Linux module-loader and systemd assumptions and redirect
        # read-only LVM paths.
        "initContainers": [
            {"name": "drbd-shutdown-guard", "$patch": "delete"},
            {"name": "drbd-module-loader", "$patch": "delete"},
        ],
        "volumes": [
            {"name": "run-systemd-system", "$patch": "delete"},
            {"name": "run-drbd-shutdown-guard", "$patch": "delete"},
            {"name": "systemd-bus-socket", "$patch": "delete"},
            {"name": "lib-modules", "$patch": "delete"},
            {"name": "usr-src", "$patch": "delete"},
            {"name": "etc-lvm-backup", "hostPath": {"path": "/var/etc/lvm/backup", "type": "DirectoryOrCreate"}},
            {"name": "etc-lvm-archive", "hostPath": {"path": "/var/etc/lvm/archive", "type": "DirectoryOrCreate"}},
        ],
    }
}

# NixOS loads DRBD 9 itself (nix/nixos/modules/piraeus-host.nix), so only the
# module loader goes; systemd and the host's LVM paths are ordinary Linux.
_NIXOS_SATELLITE_POD_TEMPLATE = {
    "spec": {
        # DRBD uses the host network; NixOS workers also register their Nebula address.
        "hostNetwork": True,
        "dnsPolicy": "ClusterFirstWithHostNet",
        "initContainers": [{"name": "drbd-module-loader", "$patch": "delete"}],
        "volumes": [{"name": "lib-modules", "$patch": "delete"}, {"name": "usr-src", "$patch": "delete"}],
    }
}


def _storage_pool(name: str, data_device: str) -> dict[str, Any]:
    return {
        "name": name,
        "lvmThinPool": {
            "volumeGroup": _VOLUME_GROUPS[name],
            "thinPool": _THIN_POOL,
            "logicalVolumeCreateArguments": ["--poolmetadatasize", "4G", "--chunksize", "64K"],
        },
        "source": {"hostDevices": [data_device]},
    }


def _satellite_configuration(
    name: str,
    node_selector: dict[str, str],
    pod_template: dict[str, Any],
    storage_pools: list[dict[str, Any]],
    *,
    properties: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    configuration: dict[str, Any] = {"name": name, "nodeSelector": node_selector}
    if properties is not None:
        configuration["properties"] = properties
    configuration.update(
        {
            "patches": _SATELLITE_PATCHES,
            "podTemplate": pod_template,
            "deletionPolicy": "Retain",
            "storagePools": storage_pools,
        }
    )
    return configuration


def _local_storage_class(name: str, pool: str, reclaim_policy: str) -> dict[str, Any]:
    return {
        "name": name,
        "reclaimPolicy": reclaim_policy,
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
        "parameters": {
            **_DISCARD_MOUNT_OPTS,
            "linstor.csi.linbit.com/storagePool": pool,
            "linstor.csi.linbit.com/placementCount": "1",
            "linstor.csi.linbit.com/allowRemoteVolumeAccess": "false",
        },
    }


def _auto_diskful_storage_class(name: str, pool: str, reclaim_policy: str) -> dict[str, Any]:
    return {
        "name": name,
        "reclaimPolicy": reclaim_policy,
        "volumeBindingMode": "WaitForFirstConsumer",
        "allowVolumeExpansion": True,
        "parameters": {
            **_DISCARD_MOUNT_OPTS,
            "linstor.csi.linbit.com/storagePool": pool,
            "linstor.csi.linbit.com/placementCount": "2",
            "linstor.csi.linbit.com/replicasOnSame": f"{node_scheduling.ZONE_LABEL}={node_scheduling.HIL_OVH_ZONE}",
            "linstor.csi.linbit.com/allowRemoteVolumeAccess": f"- fromSame:\n  - {node_scheduling.ZONE_LABEL}\n",
            # Plain `DrbdOptions/...` keys, as proven on the auto-diskful canary
            # (cluster/debug/drbd_canary/auto_diskful/).
            "DrbdOptions/auto-quorum": "suspend-io",
            "DrbdOptions/auto-diskful": "1",
            "DrbdOptions/auto-diskful-allow-cleanup": "true",
        },
    }


def operator_chart(app: App) -> Chart:
    """Install the pinned operator and its CRDs before the separate cluster chart."""
    chart = Chart(app, "piraeus-operator", disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND, labels=PRIVILEGED_POD_SECURITY)
    repository = oci_helm_repository(
        chart, "piraeus-operator", "flux-system", url="oci://ghcr.io/piraeusdatastore/piraeus-operator"
    )
    helm_release(
        chart,
        "piraeus-operator",
        NAMESPACE,
        repository=repository,
        chart="piraeus",
        version="2.12.0",
        interval="30m",
        timeout="10m",
        install=RETRY_FAILED_INSTALL,
        values={
            "installCRDs": True,
            "operator": {"resources": {"requests": {"cpu": "200m", "memory": "192Mi"}}},
            # The controller is cluster-wide, but keep it on the existing OVH region.
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            # A single operator replica can be restarted during node maintenance.
            "podDisruptionBudget": {"enabled": False},
        },
    )
    return chart


def cluster_chart(app: App) -> Chart:
    """Declare LINSTOR, the Talos satellite patch, and the SSD and HDD classes."""
    chart = Chart(app, "piraeus-cluster", disable_resource_name_hashes=True)
    ConfigMap(
        chart,
        "drbd-alloy-config",
        metadata=ApiObjectMetadata(
            name=_DRBD_ALLOY_CONFIG_MAP,
            namespace=NAMESPACE,
            annotations={"description": "Alloy scrape of the loopback-only DRBD Reactor metrics endpoint."},
        ),
        data={"config.alloy": _DRBD_ALLOY_CONFIG},
    )
    k8s.KubeDaemonSet(
        chart,
        "drbd-metrics-daemonset",
        metadata=k8s.ObjectMeta(name="piraeus-drbd-metrics", namespace=NAMESPACE, labels=_DRBD_ALLOY_LABELS),
        spec=k8s.DaemonSetSpec(
            selector=k8s.LabelSelector(match_labels=_DRBD_ALLOY_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_DRBD_ALLOY_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    host_network=True,
                    dns_policy="ClusterFirstWithHostNet",
                    affinity=k8s.Affinity(
                        node_affinity=k8s.NodeAffinity(
                            required_during_scheduling_ignored_during_execution=k8s.NodeSelector(
                                node_selector_terms=[
                                    k8s.NodeSelectorTerm(
                                        match_expressions=[
                                            k8s.NodeSelectorRequirement(
                                                key=_PIRAEUS_READY_LABEL, operator="In", values=["true"]
                                            )
                                        ]
                                    )
                                ]
                            )
                        )
                    ),
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION, node_scheduling.ROAMING_TOLERATION],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=473,
                        run_as_group=473,
                        fs_group=473,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    termination_grace_period_seconds=30,
                    volumes=[
                        k8s.Volume(
                            name="config",
                            config_map=k8s.ConfigMapVolumeSource(name=_DRBD_ALLOY_CONFIG_MAP, default_mode=0o444),
                        ),
                        k8s.Volume(
                            name="storage",
                            empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("512Mi")),
                        ),
                        k8s.Volume(
                            name="tmp", empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("16Mi"))
                        ),
                    ],
                    containers=[
                        k8s.Container(
                            name="alloy",
                            image=_DRBD_ALLOY_IMAGE,
                            image_pull_policy="IfNotPresent",
                            args=[
                                "run",
                                "/etc/alloy/config.alloy",
                                "--storage.path=/var/lib/alloy",
                                "--server.http.listen-addr=127.0.0.1:12346",
                                "--stability.level=generally-available",
                                "--disable-reporting",
                            ],
                            env=[
                                k8s.EnvVar(name="MIMIR_PUSH_URL", value=mimir.PUSH_URL),
                                k8s.EnvVar(
                                    name="NODE_NAME",
                                    value_from=k8s.EnvVarSource(
                                        field_ref=k8s.ObjectFieldSelector(field_path="spec.nodeName")
                                    ),
                                ),
                            ],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=True,
                                run_as_non_root=True,
                                run_as_user=473,
                                run_as_group=473,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            # The limit covers heap plus Alloy's mapped binary. At 128Mi
                            # (~35Mi heap, ~90Mi page cache) the kernel kept evicting the
                            # binary's text and the process faulted it straight back: on
                            # ovh-ns103711 that read ~150 MB/s off containerd's HDD for 16 h,
                            # stalled every image pull there, and scraped nothing, while the
                            # pod stayed Ready. See
                            # docs/lessons_learned/2026_09_07_eso_memory_limit_thrash.md.
                            resources=k8s.ResourceRequirements(
                                requests=quantities({"cpu": "10m", "memory": "256Mi", "ephemeral-storage": "64Mi"}),
                                limits=quantities({"cpu": "250m", "memory": "512Mi", "ephemeral-storage": "640Mi"}),
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/etc/alloy", read_only=True),
                                k8s.VolumeMount(name="storage", mount_path="/var/lib/alloy"),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                            ],
                        )
                    ],
                ),
            ),
        ),
    )
    k8s.KubeNetworkPolicy(
        chart,
        "controller-api-ingress",
        metadata=k8s.ObjectMeta(
            name="linstor-controller-api",
            namespace=NAMESPACE,
            annotations={"description": "Restrict the LINSTOR controller API to mutually authenticated HTTPS."},
        ),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_CONTROLLER_POD_SELECTOR),
            policy_types=["Ingress"],
            # Cilium's local-host exemption keeps node-local health probes on 3370 working.
            # Ordinary Pods may reach only the API-TLS listener; LINSTOR mTLS authenticates clients.
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(3371), protocol="TCP")]
                )
            ],
        ),
    )
    piraeus_metrics.add_thin_pool_monitoring(
        chart,
        namespace=NAMESPACE,
        node_selector=_FIRST_NODE_SELECTOR,
        volume_group=_VOLUME_GROUPS["ssd"],
        thin_pool=_THIN_POOL,
        storage_pool="ssd",
    )
    for monitor_id, node_selector, storage_pool, name, ready_node in (
        ("next-node-metrics", _NEXT_NODE_SELECTOR, "ssd", "piraeus-lvm-metrics-ovh-ns104952", None),
        ("third-node-metrics", _THIRD_NODE_SELECTOR, "ssd", "piraeus-lvm-metrics-ovh-ns104963", None),
        ("hdd-first-node-metrics", _HDD_FIRST_NODE_SELECTOR, "hdd", "piraeus-lvm-metrics-ovh-ns103656", None),
        ("hdd-next-node-metrics", _HDD_NEXT_NODE_SELECTOR, "hdd", "piraeus-lvm-metrics-ovh-ns103711", None),
        ("hdd-last-node-metrics", _HDD_LAST_NODE_SELECTOR, "hdd", "piraeus-lvm-metrics-ovh-ns102453", None),
        ("optiplex-metrics", _OPTIPLEX_SELECTOR, "ssd", "piraeus-lvm-metrics-optiplex", None),
        ("wyrm2-ssd-metrics", _WYRM2_SELECTOR, "ssd", "piraeus-lvm-metrics-wyrm2-ssd", None),
        ("wyrm2-hdd-metrics", _WYRM2_SELECTOR, "hdd", "piraeus-lvm-metrics-wyrm2-hdd", None),
        ("rugged-metrics", _RUGGED_SELECTOR, "ssd", "piraeus-lvm-metrics-rugged", "rugged"),
    ):
        piraeus_metrics.add_thin_pool_monitoring(
            Construct(chart, monitor_id),
            namespace=NAMESPACE,
            node_selector=node_selector,
            volume_group=_VOLUME_GROUPS[storage_pool],
            thin_pool=_THIN_POOL,
            storage_pool=storage_pool,
            name=name,
            only_when_node_ready=ready_node,
        )
    repository = oci_helm_repository(
        chart, "piraeus-charts", "flux-system", url="oci://ghcr.io/piraeusdatastore/helm-charts"
    )
    helm_release(
        chart,
        "linstor-cluster",
        NAMESPACE,
        repository=repository,
        chart="linstor-cluster",
        version="1.3.2",
        interval="30m",
        timeout="10m",
        install=RETRY_FAILED_INSTALL,
        values={
            "monitoring": {"enabled": True, "dashboard": {"enabled": False}},
            # The chart and operator create cert-manager resources and private keys at
            # runtime; no certificate or key material is stored in Git.
            "createInternalTLS": "cert-manager",
            "createApiTLS": "cert-manager",
            "linstorCluster": {
                # Admit canaries and workers explicitly prepared to join Piraeus.
                "nodeAffinity": _CLUSTER_NODE_AFFINITY,
                # Satellites and CSI node Pods run on the laptops too, once one is labelled ready.
                "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION, node_scheduling.ROAMING_TOLERATION],
                "properties": [{"name": "TcpPortAutoRange", "value": "7900-7999"}],
                "controller": {"enabled": True, "podTemplate": {"spec": {"nodeSelector": _CONTROLLER_NODE_SELECTOR}}},
                "csiController": {
                    "enabled": True,
                    "podTemplate": {"spec": {"nodeSelector": _CONTROLLER_NODE_SELECTOR}},
                },
                "csiNode": {"enabled": True},
                # Keep local PV affinity updates; omit the unrelated NFS server.
                "affinityController": {
                    "enabled": True,
                    "podTemplate": {
                        "spec": {
                            "nodeSelector": _CONTROLLER_NODE_SELECTOR,
                            "containers": [
                                {
                                    "name": "linstor-affinity-controller",
                                    "resources": {"requests": {"cpu": "15m", "memory": "64Mi"}},
                                }
                            ],
                        }
                    },
                },
                # Force-deletes Pods and VolumeAttachments stuck on a node whose DRBD primary lost
                # quorum, so replicated volumes fail over without manual force-deletes.
                "highAvailabilityController": {"enabled": True},
                "nfsServer": {"enabled": False},
            },
            "linstorSatelliteConfigurations": [
                # This selector becomes eligible only after the target rebuild config
                # adds piraeus-ready=true following data evacuation and disk wipe.
                # source.hostDevices lets Piraeus initialize the entire data device.
                _satellite_configuration(
                    "ssd-first-node",
                    _FIRST_NODE_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("ssd", _FIRST_NODE_DATA_DEVICE)],
                ),
                # Keep both the hostname and readiness label in the native selector.
                # The source device is initialized only after a later cutover adds the label.
                _satellite_configuration(
                    "ssd-ovh-ns104952",
                    _NEXT_NODE_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("ssd", _NEXT_NODE_DATA_DEVICE)],
                ),
                # Keep both the hostname and readiness label in the native selector.
                # The source device is initialized only after a later cutover adds the label.
                _satellite_configuration(
                    "ssd-ovh-ns104963",
                    _THIRD_NODE_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("ssd", _THIRD_NODE_DATA_DEVICE)],
                ),
                _satellite_configuration(
                    "ssd-optiplex",
                    # The raw partition is Talos-owned. This selector remains inert
                    # until its parent disk and 112GiB size are verified after reimage.
                    _OPTIPLEX_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("ssd", _OPTIPLEX_DATA_DEVICE)],
                ),
                # Keep the longer external-command wait scoped to this HDD satellite.
                _satellite_configuration(
                    "hdd-ovh-ns103656",
                    # This remains inert until the operator enables piraeus-ready after
                    # the worker's HDD has been wiped and its physical acceptance is complete.
                    _HDD_FIRST_NODE_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("hdd", _HDD_FIRST_NODE_DATA_DEVICE)],
                    properties=_HDD_PROPERTIES,
                ),
                _satellite_configuration(
                    "hdd-ovh-ns103711",
                    # This remains inert until the old HDD is wiped and its physical
                    # layout is accepted; piraeus-ready is enabled only afterward.
                    _HDD_NEXT_NODE_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("hdd", _HDD_NEXT_NODE_DATA_DEVICE)],
                    properties=_HDD_PROPERTIES,
                ),
                _satellite_configuration(
                    "hdd-ovh-ns102453",
                    # This remains inert until the old HDD is wiped and its physical
                    # layout is accepted; piraeus-ready is enabled only afterward.
                    _HDD_LAST_NODE_SELECTOR,
                    _TALOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("hdd", _HDD_LAST_NODE_DATA_DEVICE)],
                    properties=_HDD_PROPERTIES,
                ),
                # Both pools sit on thin Atlas zvols whose free space LINSTOR cannot see.
                # The HDD pool's external commands get the same longer wait as OVH HDDs.
                _satellite_configuration(
                    "wyrm2",
                    _WYRM2_SELECTOR,
                    _NIXOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("ssd", _WYRM2_SSD_DATA_DEVICE), _storage_pool("hdd", _WYRM2_HDD_DATA_DEVICE)],
                    properties=_HDD_PROPERTIES,
                ),
                # A roaming laptop: only strict-local `piraeus-ssd-local` claims land here (the
                # replicated class keeps copies in hil-ovh), and they go offline with the laptop.
                _satellite_configuration(
                    "rugged",
                    _RUGGED_SELECTOR,
                    _NIXOS_SATELLITE_POD_TEMPLATE,
                    [_storage_pool("ssd", _RUGGED_DATA_DEVICE)],
                ),
            ],
            "storageClasses": [
                *(
                    _local_storage_class(name, pool, reclaim_policy)
                    for name, (pool, reclaim_policy) in piraeus_storage.LOCAL_CLASSES.items()
                ),
                # Two synchronous copies (DRBD's default protocol C) on distinct OVH SSD nodes,
                # attachable disklessly from any OVH node, so a single-writer app keeps Seaweed's
                # two-copy, run-anywhere behavior. Copies and attachments stay in hil-ovh, never
                # replicating over the home/OVH link (optiplex also has an `ssd` pool). LINSTOR
                # adds a diskless tiebreaker on a third node; losing quorum suspends I/O rather
                # than letting a partitioned copy diverge. Auto-diskful gives the consuming node its
                # own copy after a minute of diskless use (where that node has an `ssd` pool), and
                # cleanup then drops the surplus copy. Meant for small volumes, where that resync
                # is trivial.
                _auto_diskful_storage_class(piraeus_storage.SSD_REPLICATED, "ssd", "Retain"),
                # Auto-diskful moves a replica to the consumer after it stays on an HDD node.
                _auto_diskful_storage_class(piraeus_storage.HDD_REPLICATED, "hdd", "Retain"),
            ],
        },
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        HelmReleaseSpecPostRenderersKustomizePatches(
                            target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(
                                group="monitoring.coreos.com", version="v1", kind="PodMonitor", name="linstor-satellite"
                            ),
                            patch=_SATELLITE_POD_MONITOR_DELETE_PATCH,
                        )
                    ]
                )
            )
        ],
    )
    return chart


def operator_kustomization(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    """Install the operator while keeping accidental Flux removal from uninstalling it."""
    return flux_kustomization(
        chart,
        "piraeus-operator",
        directory,
        timeout="15m",
        prune=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
    )


def cluster_kustomization(
    chart: Chart,
    directory: RenderedDirectory,
    operator: Kustomization,
    cert_manager: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    """Apply LINSTOR only after its CRDs and cert-manager are Ready."""
    return flux_kustomization(
        chart,
        "piraeus-cluster",
        directory,
        timeout="15m",
        prune=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(operator, cert_manager, monitoring_crds),
    )
