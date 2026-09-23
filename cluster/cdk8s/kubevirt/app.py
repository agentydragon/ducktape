"""The KubeVirt CR that virt-operator (cluster/k8s/kubevirt/operator) reconciles into
the KubeVirt control plane, and the Flux Kustomization that waits for that plane."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from kubevirt_kubevirt_crds.io.kubevirt import (
    KubeVirt,
    KubeVirtSpec,
    KubeVirtSpecCertificateRotateStrategy,
    KubeVirtSpecConfiguration,
    KubeVirtSpecConfigurationDeveloperConfiguration,
    KubeVirtSpecConfigurationPermittedHostDevices,
    KubeVirtSpecConfigurationPermittedHostDevicesUsb,
    KubeVirtSpecConfigurationPermittedHostDevicesUsbSelectors,
    KubeVirtSpecConfigurationSmbios,
    KubeVirtSpecCustomizeComponents,
    KubeVirtSpecInfra,
    KubeVirtSpecInfraNodePlacement,
    KubeVirtSpecWorkloads,
    KubeVirtSpecWorkloadsNodePlacement,
    KubeVirtSpecWorkloadsNodePlacementAffinity,
    KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinity,
    KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution,
    KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms,
    KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions,
    KubeVirtSpecWorkloadUpdateStrategy,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "kubevirt"
NAMESPACE = "kubevirt"
OUTPUT_DIR = "cluster/k8s/kubevirt/app"
_IF_NOT_PRESENT = "IfNotPresent"


def chart(app: App) -> Chart:
    chart = Chart(app, "kubevirt", disable_resource_name_hashes=True)
    KubeVirt(
        chart,
        "kubevirt",
        metadata=metadata(
            NAME,
            NAMESPACE,
            annotations={
                "description": "KubeVirt control plane for running virtual machines on KVM-capable Kubernetes workers."
            },
        ),
        spec=KubeVirtSpec(
            certificate_rotate_strategy=KubeVirtSpecCertificateRotateStrategy(),
            configuration=KubeVirtSpecConfiguration(
                developer_configuration=KubeVirtSpecConfigurationDeveloperConfiguration(feature_gates=["HostDevices"]),
                image_pull_policy=_IF_NOT_PRESENT,
                permitted_host_devices=KubeVirtSpecConfigurationPermittedHostDevices(
                    usb=[
                        KubeVirtSpecConfigurationPermittedHostDevicesUsb(
                            resource_name="kubevirt.io/cpap-wifi",
                            selectors=[
                                KubeVirtSpecConfigurationPermittedHostDevicesUsbSelectors(vendor="0e8d", product="7961")
                            ],
                        )
                    ]
                ),
                smbios=KubeVirtSpecConfigurationSmbios(
                    manufacturer="Talos Virtualization",
                    product="allegedly-works-kubevirt",
                    sku="allegedly-works",
                    version="v0.1.0",
                ),
            ),
            customize_components=KubeVirtSpecCustomizeComponents(),
            image_pull_policy=_IF_NOT_PRESENT,
            infra=KubeVirtSpecInfra(
                node_placement=KubeVirtSpecInfraNodePlacement(node_selector={"topology.kubernetes.io/region": "hil"})
            ),
            workloads=KubeVirtSpecWorkloads(
                node_placement=KubeVirtSpecWorkloadsNodePlacement(
                    affinity=KubeVirtSpecWorkloadsNodePlacementAffinity(
                        node_affinity=KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinity(
                            required_during_scheduling_ignored_during_execution=KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecution(
                                node_selector_terms=[
                                    KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTerms(
                                        match_expressions=[
                                            KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                                key="topology.kubernetes.io/region",
                                                operator="In",
                                                values=["hil", "home", "proxmox"],
                                            ),
                                            KubeVirtSpecWorkloadsNodePlacementAffinityNodeAffinityRequiredDuringSchedulingIgnoredDuringExecutionNodeSelectorTermsMatchExpressions(
                                                key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                                            ),
                                        ]
                                    )
                                ]
                            )
                        )
                    )
                )
            ),
            workload_update_strategy=KubeVirtSpecWorkloadUpdateStrategy(),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def kubevirt(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kubevirt_operator: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        timeout="10m",
        depends_on=[flux_kustomization_depends_on(kubevirt_operator)],
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="virt-api", namespace=NAMESPACE
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="virt-controller", namespace=NAMESPACE
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="DaemonSet", name="virt-handler", namespace=NAMESPACE
            ),
        ],
    )
