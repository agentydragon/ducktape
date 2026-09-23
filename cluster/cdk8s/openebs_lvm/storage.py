"""OpenEBS LVM LocalPV on the Proxmox node, its StorageClasses and default
VolumeSnapshotClass, and the directory's Flux Kustomization.

No class here constrains provisioning to Proxmox nodes: the LVM CSI driver advertises only
the `kubernetes.io/hostname` and `openebs.io/nodename` topology keys, so an
`allowedTopologies` on `topology.kubernetes.io/region=proxmox` cannot be used. The implicit
constraint is the lvmNode DaemonSet's `nodeSelector` (region=proxmox) -- provisioning fails
on other nodes -- so consumers pin their pods to region=proxmox to avoid failed scheduling.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_snapshotter_volumesnapshotclass_crds.io.k8s.storage.snapshot import (
    VolumeSnapshotClass,
    VolumeSnapshotClassDeletionPolicy,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release
from cluster.cdk8s.metadata import metadata

NAME = "openebs-lvm"
NAMESPACE = "openebs"
RELEASE = "openebs-lvm-localpv"
OUTPUT_DIR = "cluster/k8s/openebs-lvm"
_PROXMOX = {"topology.kubernetes.io/region": "proxmox"}
_DRIVER = "local.csi.openebs.io"


def _storage_class(scope: Construct, name: str, *, parameters: dict[str, str]) -> None:
    k8s.KubeStorageClass(
        scope,
        name,
        metadata=k8s.ObjectMeta(name=name),
        provisioner=_DRIVER,
        reclaim_policy="Delete",
        volume_binding_mode="WaitForFirstConsumer",
        allow_volume_expansion=True,
        parameters={"storage": "lvm", **parameters},
    )


def _storage_classes(scope: Construct) -> None:
    for tier in ("ssd", "hdd"):
        volgroup = f"openebs-proxmox-{tier}"
        _storage_class(
            scope, f"lvm-proxmox-{tier}", parameters={"volgroup": volgroup, "fstype": "ext4", "thinProvision": "yes"}
        )
        _storage_class(scope, f"lvm-proxmox-{tier}-block", parameters={"volgroup": volgroup, "thinProvision": "yes"})
    _storage_class(
        scope,
        "lvm-proxmox-hdd-shared",
        parameters={"volgroup": "openebs-proxmox-hdd", "fstype": "ext4", "thinProvision": "yes", "shared": "yes"},
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={
                "pod-security.kubernetes.io/enforce": "privileged",
                "pod-security.kubernetes.io/audit": "privileged",
                "pod-security.kubernetes.io/warn": "privileged",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )
    repository = HelmRepository(
        chart,
        "repository",
        metadata=metadata(RELEASE, "flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://openebs.github.io/lvm-localpv"),
    )
    helm_release(
        chart,
        RELEASE,
        NAMESPACE,
        repository=repository,
        chart="lvm-localpv",
        version="1.10.1",
        interval="30m",
        install=RETRY_FAILED_INSTALL,
        values={"lvmNode": {"nodeSelector": _PROXMOX}, "lvmController": {"nodeSelector": _PROXMOX}},
    )
    _storage_classes(chart)
    VolumeSnapshotClass(
        chart,
        "snapshot-class",
        metadata=ApiObjectMetadata(
            name="openebs-lvm-snapclass", annotations={"snapshot.storage.kubernetes.io/is-default-class": "true"}
        ),
        driver=_DRIVER,
        deletion_policy=VolumeSnapshotClassDeletionPolicy.DELETE,
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def openebs_lvm(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, timeout="5m")
