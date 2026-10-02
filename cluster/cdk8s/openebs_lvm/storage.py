"""OpenEBS LVM LocalPV on the Proxmox node, its StorageClasses and default
VolumeSnapshotClass, and the directory's Flux Kustomization.

No class here constrains provisioning to Proxmox nodes: the LVM CSI driver advertises only
the `kubernetes.io/hostname` and `openebs.io/nodename` topology keys, so an
`allowedTopologies` on `topology.kubernetes.io/region=proxmox` cannot be used. The implicit
constraint is the lvmNode DaemonSet's `nodeSelector` (region=proxmox) -- provisioning fails
on other nodes -- so consumers pin their pods to region=proxmox to avoid failed scheduling.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_snapshotter_volumesnapshotclass_crds.io.k8s.storage.snapshot import (
    VolumeSnapshotClass,
    VolumeSnapshotClassDeletionPolicy,
)

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "openebs-lvm"
NAMESPACE = "openebs"
RELEASE = "openebs-lvm-localpv"
OUTPUT_DIR = f"{GENERATED_ROOT}/openebs-lvm"
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
        RELEASE,
        NAMESPACE,
        repository=https_helm_repository(chart, RELEASE, "flux-system", url="https://openebs.github.io/lvm-localpv"),
        chart="lvm-localpv",
        version="1.10.1",
        interval="30m",
        install=RETRY_FAILED_INSTALL,
        values={
            "lvmNode": {"nodeSelector": _PROXMOX},
            "lvmController": {"nodeSelector": _PROXMOX},
            # snapshot-controller-crds, which this unit depends on, owns the VolumeSnapshot CRDs.
            # Helm refuses to install over a resource it does not own, so the chart's own
            # copies of those CRDs would fail the release.
            "crds": {"csi": {"volumeSnapshots": {"enabled": False}}},
        },
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


def openebs_lvm(chart: Chart, directory: RenderedDirectory, snapshot_controller_crds: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="5m", depends_on=[flux_kustomization_depends_on(snapshot_controller_crds)]
    )
