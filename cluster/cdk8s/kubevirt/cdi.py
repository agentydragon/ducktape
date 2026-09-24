"""The cluster-scoped CDI CR that cdi-operator (cluster/k8s/kubevirt/cdi-operator)
reconciles into the Containerized Data Importer, and the StorageProfiles for our
provisioners."""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs
from kubevirt_cdi_crds.io.kubevirt.cdi import (
    Cdi,
    CdiSpec,
    CdiSpecConfig,
    CdiSpecConfigPodResourceRequirements,
    CdiSpecConfigPodResourceRequirementsLimits,
    CdiSpecConfigPodResourceRequirementsRequests,
    CdiSpecImagePullPolicy,
    CdiSpecInfra,
    CdiSpecWorkload,
    CdiSpecWorkloadAffinity,
    CdiSpecWorkloadAffinityNodeAffinity,
    CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
)
from kubevirt_storageprofile_crds.io.kubevirt.cdi import (
    StorageProfile,
    StorageProfileSpec,
    StorageProfileSpecClaimPropertySets,
    StorageProfileSpecClaimPropertySetsVolumeMode,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/kubevirt/cdi"
_FILESYSTEM = StorageProfileSpecClaimPropertySetsVolumeMode.FILESYSTEM
_BLOCK = StorageProfileSpecClaimPropertySetsVolumeMode.BLOCK
# storage class -> (clone strategy, [(access mode, volume mode)])
_STORAGE_PROFILES = {
    "local-path-ovh": ("copy", [("ReadWriteOnce", _FILESYSTEM)]),
    "local-path-proxmox": ("copy", [("ReadWriteOnce", _FILESYSTEM)]),
    "lvm-proxmox-hdd": ("snapshot", [("ReadWriteOnce", _FILESYSTEM)]),
    "lvm-proxmox-hdd-block": ("snapshot", [("ReadWriteOnce", _BLOCK)]),
    "lvm-proxmox-hdd-shared": ("snapshot", [("ReadWriteOnce", _FILESYSTEM)]),
    "lvm-proxmox-ssd": ("snapshot", [("ReadWriteOnce", _FILESYSTEM)]),
    "lvm-proxmox-ssd-block": ("snapshot", [("ReadWriteOnce", _BLOCK)]),
    "seaweedfs-ovh": ("copy", [("ReadWriteMany", _FILESYSTEM), ("ReadWriteOnce", _FILESYSTEM)]),
}


def chart(app: App) -> Chart:
    chart = Chart(app, "cdi", disable_resource_name_hashes=True)
    Cdi(
        chart,
        "cdi",
        metadata=ApiObjectMetadata(
            name="cdi", annotations={"description": "Containerized Data Importer for KubeVirt VM disk image imports."}
        ),
        spec=CdiSpec(
            config=CdiSpecConfig(
                feature_gates=["HonorWaitForFirstConsumer", "WebhookPvcRendering"],
                scratch_space_storage_class="local-path-ovh",
                pod_resource_requirements=CdiSpecConfigPodResourceRequirements(
                    requests={
                        "cpu": CdiSpecConfigPodResourceRequirementsRequests.from_string("100m"),
                        "memory": CdiSpecConfigPodResourceRequirementsRequests.from_string("128Mi"),
                    },
                    limits={
                        "cpu": CdiSpecConfigPodResourceRequirementsLimits.from_string("750m"),
                        "memory": CdiSpecConfigPodResourceRequirementsLimits.from_string("2Gi"),
                    },
                ),
            ),
            image_pull_policy=CdiSpecImagePullPolicy.IF_NOT_PRESENT,
            infra=CdiSpecInfra(node_selector={"topology.kubernetes.io/region": "hil"}),
            workload=CdiSpecWorkload(
                affinity=CdiSpecWorkloadAffinity(
                    node_affinity=CdiSpecWorkloadAffinityNodeAffinity(
                        required_during_scheduling_ignored_during_execution=CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                            node_selector_terms=[
                                CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                    match_expressions=[
                                        CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                            key="topology.kubernetes.io/region",
                                            operator="In",
                                            values=["hil", "home", "proxmox"],
                                        ),
                                        CdiSpecWorkloadAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                            key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                                        ),
                                    ]
                                )
                            ]
                        )
                    )
                )
            ),
        ),
    )
    # CDI creates StorageProfile objects automatically, but it cannot infer PVC claim
    # properties for our local/custom provisioners. Declare the safe defaults so
    # DataVolumes do not have to guess access mode or volume mode.
    for name, (clone_strategy, claim_property_sets) in _STORAGE_PROFILES.items():
        StorageProfile(
            chart,
            name,
            metadata=ApiObjectMetadata(name=name),
            spec=StorageProfileSpec(
                claim_property_sets=[
                    StorageProfileSpecClaimPropertySets(access_modes=[access_mode], volume_mode=volume_mode)
                    for access_mode, volume_mode in claim_property_sets
                ],
                clone_strategy=clone_strategy,
            ),
        )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def cdi(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cdi_operator: Kustomization,
    local_path_provisioner: Kustomization,
) -> Kustomization:
    name = "cdi"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        depends_on=flux_kustomization_depends_on_many(cdi_operator, local_path_provisioner),
        # The CDI CR has no Ready condition, so `wait` alone passes it before cdi-operator rolls
        # out cdi-apiserver/-deployment/-uploadproxy. The operator enters phase Deployed with
        # Available=True once none of them is degraded, and phase Error on failure
        # (controller-lifecycle-operator-sdk v0.2.7 pkg/sdk/reconciler/reconciler.go,
        # pkg/sdk/cr-status.go MarkCrHealthyMessage; CDI v1.65.0 embeds its api.Status).
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="cdi.kubevirt.io/v1beta1",
                kind="CDI",
                current=(
                    "has(status.phase) && status.phase == 'Deployed' && has(status.conditions) && "
                    "status.conditions.exists(c, c.type == 'Available' && c.status == 'True')"
                ),
                failed="has(status.phase) && status.phase == 'Error'",
            )
        ],
    )
