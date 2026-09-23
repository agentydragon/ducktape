"""The descheduler in kube-system: its HelmRepository, the HelmRelease that installs
the chart with the cluster's eviction policy as `values`, and a PVC-read grant beside
the chart's own RBAC.

`values` is the one untyped block: the descheduler chart ships no `values.schema.json`,
and its `DeschedulerPolicy` is a Go type with no CRD for `cdk8s_import` to ingest.
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

from cluster.cdk8s import stateful_infra
from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata

NAME = "descheduler"
NAMESPACE = "kube-system"
OUTPUT_DIR = "cluster/k8s/descheduler"
_PVC_READER = "descheduler-pvc"


def _values() -> dict[str, object]:
    return {
        "schedule": "*/15 * * * *",
        "deschedulerPolicyAPIVersion": "descheduler/v1alpha2",
        "deschedulerPolicy": {
            "metricsProviders": [{"source": "KubernetesMetrics"}],
            "profiles": [
                {
                    "name": "default",
                    "pluginConfig": [
                        {
                            "name": "DefaultEvictor",
                            "args": {
                                # Without nodeFit, evicted pods that fit nowhere else reschedule
                                # onto the same node and get evicted again next run -- a 15-minute
                                # churn loop whenever other nodes are down or unschedulable.
                                "nodeFit": True,
                                # Pods at or above this priority are never evicted, by any plugin.
                                # PriorityClass alone does not do this: only
                                # system-{cluster,node}-critical are exempt by default, and a lower
                                # class merely loses the QoS tiebreak. SeaweedFS master/volume/
                                # filer/s3 must be exempt outright: a filer restart desynchronizes
                                # every FUSE client's cached chunk locations, and git then dies of
                                # SIGBUS on mmap'd packfiles cluster-wide. Given as a value rather
                                # than the class name: the PriorityClass belongs to another Flux
                                # Kustomization (seaweedfs-cluster).
                                "priorityThreshold": {"value": stateful_infra.PRIORITY},
                            },
                        },
                        {
                            "name": "LowNodeUtilization",
                            "args": {
                                "metricsUtilization": {"source": "KubernetesMetrics"},
                                "thresholds": {"cpu": 20, "memory": 20},
                                "targetThresholds": {"cpu": 50, "memory": 70},
                            },
                        },
                        {"name": "RemoveDuplicates", "args": {}},
                        {
                            "name": "RemovePodsHavingTooManyRestarts",
                            "args": {"podRestartThreshold": 50, "includingInitContainers": True},
                        },
                    ],
                    "plugins": {
                        "balance": {"enabled": ["LowNodeUtilization", "RemoveDuplicates"]},
                        "deschedule": {"enabled": ["RemovePodsHavingTooManyRestarts"]},
                    },
                }
            ],
        },
        "resources": {"requests": {"cpu": "25m", "memory": "128Mi"}, "limits": {"cpu": "200m", "memory": "256Mi"}},
        "rbac": {"create": True},
    }


class Descheduler(Construct):
    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        repository = HelmRepository(
            self,
            "repository",
            metadata=metadata(NAME, NAMESPACE),
            spec=HelmRepositorySpec(interval="24h", url="https://kubernetes-sigs.github.io/descheduler"),
        )
        HelmRelease(
            self,
            "release",
            metadata=metadata(NAME, NAMESPACE),
            spec=HelmReleaseSpec(
                interval="30m",
                install=HelmReleaseSpecInstall(remediation=HelmReleaseSpecInstallRemediation(retries=3)),
                chart=HelmReleaseSpecChart(
                    spec=HelmReleaseSpecChartSpec(
                        chart="descheduler",
                        # renovate: datasource=helm depName=descheduler registryUrl=https://kubernetes-sigs.github.io/descheduler
                        version="0.36.0",
                        source_ref=HelmReleaseSpecChartSpecSourceRef(
                            kind=HelmReleaseSpecChartSpecSourceRefKind.HELM_REPOSITORY,
                            name=repository.name,
                            namespace=repository.metadata.namespace,
                        ),
                    )
                ),
                values=_values(),
            ),
        )


class PvcReader(Construct):
    """Read access to PersistentVolumeClaims for the chart's ServiceAccount, which the
    chart names after the release."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        role = k8s.KubeClusterRole(
            self,
            "role",
            metadata=k8s.ObjectMeta(name=_PVC_READER),
            rules=[
                k8s.PolicyRule(api_groups=[""], resources=["persistentvolumeclaims"], verbs=["get", "list", "watch"])
            ],
        )
        k8s.KubeClusterRoleBinding(
            self,
            "binding",
            metadata=k8s.ObjectMeta(name=_PVC_READER),
            role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind=role.kind, name=role.name),
            subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=NAMESPACE)],
        )


def chart(app: App) -> Chart:
    chart = Chart(app, "helmrelease", disable_resource_name_hashes=True)
    Descheduler(chart, "descheduler")
    return chart


def rbac_chart(app: App) -> Chart:
    chart = Chart(app, "rbac", disable_resource_name_hashes=True)
    PvcReader(chart, "pvc-reader")
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart, rbac_chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["helmrelease.k8s.yaml", "rbac.k8s.yaml"]),
    )


def descheduler(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            depends_on=[flux_kustomization_depends_on(kyverno)],
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="helm.toolkit.fluxcd.io/v2", kind="HelmRelease", name=NAME, namespace=NAMESPACE
                )
            ],
        ),
    )
