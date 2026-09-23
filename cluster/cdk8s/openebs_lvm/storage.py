"""OpenEBS LVM LocalPV on the Proxmox node and its StorageClasses, written beside the
hand-written VolumeSnapshotClass (no `snapshot.storage.k8s.io` binding yet) that the
directory's `kustomization.yaml` also lists.

No class here constrains provisioning to Proxmox nodes: the LVM CSI driver advertises only
the `kubernetes.io/hostname` and `openebs.io/nodename` topology keys, so an
`allowedTopologies` on `topology.kubernetes.io/region=proxmox` cannot be used. The implicit
constraint is the lvmNode DaemonSet's `nodeSelector` (region=proxmox) -- provisioning fails
on other nodes -- so consumers pin their pods to region=proxmox to avoid failed scheduling.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmRelease,
    HelmReleaseSpec,
    HelmReleaseSpecChart,
    HelmReleaseSpecChartSpec,
    HelmReleaseSpecChartSpecSourceRef,
    HelmReleaseSpecChartSpecSourceRefKind,
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallRemediation,
)
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec

from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "openebs-lvm"
NAMESPACE = "openebs"
RELEASE = "openebs-lvm-localpv"
OUTPUT_DIR = "cluster/k8s/openebs-lvm"
_PROXMOX = {"topology.kubernetes.io/region": "proxmox"}


def _storage_class(scope: Construct, name: str, *, parameters: dict[str, str]) -> None:
    k8s.KubeStorageClass(
        scope,
        name,
        metadata=k8s.ObjectMeta(name=name),
        provisioner="local.csi.openebs.io",
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
    HelmRelease(
        chart,
        "release",
        metadata=metadata(RELEASE, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart="lvm-localpv",
                    version="1.10.1",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values={"lvmNode": {"nodeSelector": _PROXMOX}, "lvmController": {"nodeSelector": _PROXMOX}},
        ),
    )
    _storage_classes(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
