"""Rancher's local-path provisioner and the region- and media-pinned StorageClasses that
provision through it.

The chart's own `local-path` StorageClass is retired (2026-06-03) in favor of these
region-pinned classes; the provisioner Deployment and `nodePathMap` stay because every class
here names `cluster.local/local-path-provisioner`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "local-path-provisioner"
NAMESPACE = "local-path-storage"
OUTPUT_DIR = f"{GENERATED_ROOT}/local-path-provisioner"
_PROVISIONER = "cluster.local/local-path-provisioner"
_REGION = "topology.kubernetes.io/region"
_TIER = "storage.allegedly.works/tier"
_OVH_SSD = "local-path-ovh-ssd"
_HOME_SSD = "local-path-home-ssd"
# Flip only during the OptiPlex cutover, after writers are stopped and the old
# PV directories have verified off-node copies. An empty nodePathMap entry then
# denies new allocations from every local-path class, including legacy aliases.
_OPTIPLEX_LOCAL_PATH_ENABLED = False
# OVH's tier=ssd nodes are exactly its control planes. CNPG callers that use the OVH SSD
# profile explicitly select its control-plane tolerance along with this class.

# OVH classes opt into these dedicated paths; unlisted nodes must not use the
# provisioner's default system-disk path. Empty paths also deny provisioning.
_OVH_NODE_PATHS = {
    # The HDD is reserved for the inert Piraeus pool configuration and later conversion.
    "ovh-ns103656": [],
    "ovh-ns103711": ["/var/mnt/local-path-ovh-hdd/local-path"],
    "ovh-ns102453": ["/var/mnt/local-path-ovh-hdd/local-path"],
    # Evacuated legacy claims stay readable; deny fresh allocations before cutover.
    "ovh-ns104952": [],
    "ovh-ns104963": [],
    # Its whole data disk belongs to Piraeus; retain the explicit fallback denial.
    "ovh-ns1001419": [],
}


def _node_path(node: str, *paths: str) -> dict[str, object]:
    return {"node": node, "paths": list(paths)}


def _storage_class(
    scope: Construct, name: str, *, reclaim_policy: ReclaimPolicy, topology: dict[str, list[str]]
) -> None:
    """A WaitForFirstConsumer class whose one topology term ANDs every `topology` label."""
    k8s.KubeStorageClass(
        scope,
        name,
        metadata=k8s.ObjectMeta(name=name),
        provisioner=_PROVISIONER,
        reclaim_policy=reclaim_policy,
        volume_binding_mode="WaitForFirstConsumer",
        allowed_topologies=[
            k8s.TopologySelectorTerm(
                match_label_expressions=[
                    k8s.TopologySelectorLabelRequirement(key=key, values=values) for key, values in topology.items()
                ]
            )
        ],
    )


class ReclaimPolicy(StrEnum):
    DELETE = "Delete"
    RETAIN = "Retain"


@dataclass(frozen=True)
class _Class:
    reclaim_policy: ReclaimPolicy
    topology: dict[str, list[str]]


_OVH_TOPOLOGY = {
    **{key: [value] for key, value in node_scheduling.HIL_OVH_NODE_SELECTOR.items()},
    "kubernetes.io/hostname": [node for node, paths in _OVH_NODE_PATHS.items() if paths],
}
_OVH_HDD = {**_OVH_TOPOLOGY, _TIER: ["hdd"]}
_CLASSES = {
    # DEPRECATED alias, re-pinned to the same media as local-path-ovh-hdd (KS-5 7200rpm HDD).
    # New OVH PVCs should target local-path-ovh-{hdd,ssd} explicitly; this stays only for the
    # ~40 existing bound PVCs referencing it. Adding the tier key means a re-provisioned PVC
    # can only land on an OVH hdd node, not silently on a KS-GAME NVMe. `allowedTopologies`
    # is mutable (only provisioner/parameters/reclaimPolicy/volumeBindingMode are immutable
    # on a StorageClass), so Flux updates it in place -- no delete needed. Existing bound
    # PVCs are unaffected (the SC is only read at provision time).
    # See cluster/docs/plans/ovh_storage_tiering.md.
    "local-path-ovh": _Class(ReclaimPolicy.DELETE, _OVH_HDD),
    # Media-scoped OVH node-local storage: KS-5 7200rpm HDD data disks. Keys are ANDed
    # in the same topology term -- zone keeps it OVH-scoped (never a non-OVH SSD node such as
    # wyrm2), and the tier is the hardware-keyed node label from
    # cluster/terraform/main/ovh-nodes.tf. Under WaitForFirstConsumer a PVC binds only where
    # an OVH hdd node is schedulable. See cluster/docs/plans/ovh_storage_tiering.md.
    "local-path-ovh-hdd": _Class(ReclaimPolicy.DELETE, _OVH_HDD),
    # Dedicated node-local storage for self-replicating data stores. The same topology as
    # local-path-ovh-hdd keeps claims on OVH HDD nodes, while Retain protects the data
    # directory from accidental PVC deletion.
    "local-path-ovh-hdd-retain": _Class(ReclaimPolicy.RETAIN, _OVH_HDD),
    # Media-scoped OVH node-local storage: KS-GAME NVMe data disks. zone (OVH-only) ANDed
    # with the hardware-keyed tier=ssd node label. A PVC on this class binds only where an
    # OVH ssd node is schedulable, else it stays Pending (loud) instead of silently landing
    # on HDD. Reserved for fsync/latency-critical data (Forgejo git, forgejo-db,
    # seaweedfs-filer-db). See cluster/docs/plans/ovh_storage_tiering.md.
    _OVH_SSD: _Class(ReclaimPolicy.DELETE, {**_OVH_TOPOLOGY, _TIER: ["ssd"]}),
    "local-path-proxmox": _Class(ReclaimPolicy.DELETE, {_REGION: ["proxmox"]}),
    # Home automation is intentionally hardware- and LAN-pinned: integrations use the
    # OptiPlex's Bluetooth radio and the home multicast domain. VolSync copies this
    # node-local state to replicated SeaweedFS storage for disaster recovery.
    _HOME_SSD: _Class(ReclaimPolicy.RETAIN, {_REGION: ["home"], _TIER: ["ssd"]}),
}
# All of them are node-local: a local-path volume is a directory on one node.
NODE_LOCAL_STORAGE_CLASSES = set(_CLASSES)


def _storage_classes(scope: Construct) -> None:
    for name, storage_class in _CLASSES.items():
        _storage_class(scope, name, reclaim_policy=storage_class.reclaim_policy, topology=storage_class.topology)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
        labels={
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, "flux-system", url="https://charts.containeroo.ch"),
        chart=NAME,
        version="0.0.38",
        interval="30m",
        install=RETRY_FAILED_INSTALL,
        values={
            "storageClass": {"create": False},
            "nodePathMap": [
                # /var/local-path-provisioner is writable on Talos and persists across reboots.
                _node_path("DEFAULT_PATH_FOR_NON_LISTED_NODES", "/var/local-path-provisioner"),
                *[_node_path(node, *paths) for node, paths in _OVH_NODE_PATHS.items()],
                # Home Assistant is deliberately tied to the physical home LAN and the
                # OptiPlex's radios. Keep its local state on this machine's SSD.
                _node_path("optiplex", *(["/var/local-path-provisioner"] if _OPTIPLEX_LOCAL_PATH_ENABLED else [])),
            ],
        },
    )
    _storage_classes(chart)
    return chart


def local_path_provisioner(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, timeout="5m")
