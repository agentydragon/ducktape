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

from cluster.cdk8s import namespaces
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
_HOME_SSD = "local-path-home-ssd"
# Flip only during the OptiPlex cutover, after writers are stopped and the old
# PV directories have verified off-node copies. An empty nodePathMap entry then
# denies new allocations from every local-path class.
_OPTIPLEX_LOCAL_PATH_ENABLED = False


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


_CLASSES = {
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
