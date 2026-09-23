"""The cluster-scoped CDI CR that cdi-operator (cluster/k8s/kubevirt/cdi-operator)
reconciles into the Containerized Data Importer.

The StorageProfiles beside it stay hand-written: there is no binding for that CRD, which
CDI creates at runtime rather than shipping as YAML.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
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

from cluster.cdk8s.generation import write_charts

OUTPUT_DIR = "cluster/k8s/kubevirt/cdi"


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
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
