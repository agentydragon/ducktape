"""Rancher's local-path provisioner and the region- and media-pinned StorageClasses that
provision through it.

The chart's own `local-path` StorageClass is retired (2026-06-03) in favor of these
region-pinned classes; the provisioner Deployment and `nodePathMap` stay because every class
here names `cluster.local/local-path-provisioner`.
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
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from flux_source.io.fluxcd.toolkit.source import HelmRepository, HelmRepositorySpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "local-path-provisioner"
NAMESPACE = "local-path-storage"
OUTPUT_DIR = "cluster/k8s/local-path-provisioner"
_PROVISIONER = "cluster.local/local-path-provisioner"
_ZONE = "topology.kubernetes.io/zone"
_REGION = "topology.kubernetes.io/region"
_TIER = "storage.allegedly.works/tier"


def _node_path(node: str, path: str) -> dict[str, object]:
    return {"node": node, "paths": [path]}


def _storage_class(scope: Construct, name: str, *, reclaim_policy: str, topology: dict[str, str]) -> None:
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
                    k8s.TopologySelectorLabelRequirement(key=key, values=[value]) for key, value in topology.items()
                ]
            )
        ],
    )


def _storage_classes(scope: Construct) -> None:
    ovh_hdd = {_ZONE: "hil-ovh", _TIER: "hdd"}
    # DEPRECATED alias, re-pinned to the same media as local-path-ovh-hdd (KS-5 7200rpm HDD).
    # New OVH PVCs should target local-path-ovh-{hdd,ssd} explicitly; this stays only for the
    # ~40 existing bound PVCs referencing it. Adding the tier key means a re-provisioned PVC
    # can only land on an OVH hdd node, not silently on a KS-GAME NVMe. `allowedTopologies`
    # is mutable (only provisioner/parameters/reclaimPolicy/volumeBindingMode are immutable
    # on a StorageClass), so Flux updates it in place -- no delete needed. Existing bound
    # PVCs are unaffected (the SC is only read at provision time).
    # See cluster/docs/plans/ovh_storage_tiering.md.
    _storage_class(scope, "local-path-ovh", reclaim_policy="Delete", topology=ovh_hdd)
    # Media-scoped OVH node-local storage: KS-5 7200rpm HDD data disks. Both keys are ANDed
    # in the same topology term -- zone keeps it OVH-scoped (never a non-OVH SSD node such as
    # wyrm2), and the tier is the hardware-keyed node label from
    # cluster/terraform/main/ovh-nodes.tf. Under WaitForFirstConsumer a PVC binds only where
    # an OVH hdd node is schedulable. See cluster/docs/plans/ovh_storage_tiering.md.
    _storage_class(scope, "local-path-ovh-hdd", reclaim_policy="Delete", topology=ovh_hdd)
    # Dedicated node-local storage for self-replicating data stores. The same topology as
    # local-path-ovh-hdd keeps claims on OVH HDD nodes, while Retain protects the data
    # directory from accidental PVC deletion.
    _storage_class(scope, "local-path-ovh-hdd-retain", reclaim_policy="Retain", topology=ovh_hdd)
    # Media-scoped OVH node-local storage: KS-GAME NVMe data disks. zone (OVH-only) ANDed
    # with the hardware-keyed tier=ssd node label. A PVC on this class binds only where an
    # OVH ssd node is schedulable, else it stays Pending (loud) instead of silently landing
    # on HDD. Reserved for fsync/latency-critical data (Forgejo git, forgejo-db,
    # seaweedfs-filer-db). See cluster/docs/plans/ovh_storage_tiering.md.
    _storage_class(scope, "local-path-ovh-ssd", reclaim_policy="Delete", topology={_ZONE: "hil-ovh", _TIER: "ssd"})
    _storage_class(scope, "local-path-proxmox", reclaim_policy="Delete", topology={_REGION: "proxmox"})
    # Home automation is intentionally hardware- and LAN-pinned: integrations use the
    # OptiPlex's Bluetooth radio and the home multicast domain. VolSync copies this
    # node-local state to replicated SeaweedFS storage for disaster recovery.
    _storage_class(scope, "local-path-home-ssd", reclaim_policy="Retain", topology={_REGION: "home", _TIER: "ssd"})


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
        metadata=metadata(NAME, "flux-system"),
        spec=HelmRepositorySpec(interval="24h", url="https://charts.containeroo.ch"),
    )
    HelmRelease(
        chart,
        "release",
        metadata=metadata(NAME, NAMESPACE),
        spec=HelmReleaseSpec(
            interval="30m",
            install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
            chart=HelmReleaseSpecChart(
                spec=HelmReleaseSpecChartSpec(
                    chart=NAME,
                    version="0.0.38",
                    source_ref=HelmReleaseSpecChartSpecSourceRef(
                        kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                        name=repository.name,
                        namespace=repository.metadata.namespace,
                    ),
                )
            ),
            values={
                "storageClass": {"create": False},
                "nodePathMap": [
                    # /var/local-path-provisioner is writable on Talos and persists across reboots.
                    _node_path("DEFAULT_PATH_FOR_NON_LISTED_NODES", "/var/local-path-provisioner"),
                    # OVH Kimsufi nodes carve a dedicated XFS data disk via Talos
                    # UserVolumeConfig (cluster/terraform/main/ovh-nodes.tf), mounted at
                    # /var/mnt/seaweedfs-data. The local-path-ovh StorageClass
                    # (allowedTopologies zone=hil-ovh) routes OVH-local PVCs here.
                    #
                    # KS-5 nodes use /dev/sdb; NVMe nodes use their second NVMe. List every
                    # node that should be eligible for local-path-ovh placement.
                    _node_path("ovh-ns103656", "/var/mnt/local-path-ovh-hdd/local-path"),
                    _node_path("ovh-ns103711", "/var/mnt/local-path-ovh-hdd/local-path"),
                    _node_path("ovh-ns102453", "/var/mnt/local-path-ovh-hdd/local-path"),
                    _node_path("ovh-ns104952", "/var/mnt/seaweedfs-data/local-path"),
                    _node_path("ovh-ns104963", "/var/mnt/seaweedfs-data/local-path"),
                    _node_path("ovh-ns1001419", "/var/mnt/seaweedfs-data/local-path"),
                    # Home Assistant is deliberately tied to the physical home LAN and the
                    # OptiPlex's radios. Keep its local state on this machine's SSD.
                    _node_path("optiplex", "/var/local-path-provisioner"),
                ],
                # Helper pods run in the privileged local-path-storage namespace, which
                # avoids PodSecurity restrictions in workload namespaces.
                "configmap": {"helperPodNamespace": NAMESPACE},
            },
        ),
    )
    _storage_classes(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def local_path_provisioner(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
                ),
                KustomizationSpecHealthChecks(api_version="apps/v1", kind="Deployment", name=NAME, namespace=NAMESPACE),
            ],
        ),
    )
