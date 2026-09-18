"""The descheduler in kube-system: its HelmRepository and the HelmRelease that installs
the chart with the cluster's eviction policy as `values`.

`values` is the one untyped block: the descheduler chart ships no `values.schema.json`,
and its `DeschedulerPolicy` is a Go type with no CRD for `cdk8s_import` to ingest.
"""

from __future__ import annotations

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

from cluster.cdk8s import stateful_infra
from cluster.cdk8s.metadata import metadata

NAME = "descheduler"
NAMESPACE = "kube-system"


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
