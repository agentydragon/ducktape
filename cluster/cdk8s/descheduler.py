"""The descheduler in kube-system: its HelmRepository, the HelmRelease that installs
the chart with the cluster's eviction policy as `values`, and a PVC-read grant beside
the chart's own RBAC.

`values` is the one untyped block: the descheduler chart ships no `values.schema.json`,
and its `DeschedulerPolicy` is a Go type with no CRD for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s import stateful_infra
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "descheduler"
NAMESPACE = "kube-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/descheduler"
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
        helm_release(
            self,
            NAME,
            NAMESPACE,
            repository=https_helm_repository(
                self, NAME, NAMESPACE, url="https://kubernetes-sigs.github.io/descheduler"
            ),
            chart="descheduler",
            # renovate: datasource=helm depName=descheduler registryUrl=https://kubernetes-sigs.github.io/descheduler
            version="0.36.0",
            interval="30m",
            install=RETRY_FAILED_INSTALL,
            values=_values(),
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


def descheduler(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, depends_on=[flux_kustomization_depends_on(kyverno)], timeout="5m")
