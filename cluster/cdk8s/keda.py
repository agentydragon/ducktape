"""KEDA, the autoscaler that scales haku-ci's runners, in its own namespace."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecPostRenderers,
    HelmReleaseSpecPostRenderersKustomize,
    HelmReleaseSpecPostRenderersKustomizePatches,
    HelmReleaseSpecPostRenderersKustomizePatchesTarget,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
    HelmReleaseSpecUpgradeRemediation,
)

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.haku_ci import runner
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, VpaProfile
from cluster.cdk8s.vpa_policy import ContainerPolicy, ControlledValues

NAME = "keda"
NAMESPACE = "keda"
_OPERATOR_NAME = "keda-operator"
METRICS_SERVER_NAME = "keda-operator-metrics-apiserver"
OUTPUT_DIR = f"{GENERATED_ROOT}/keda"


def _resources(
    *, cpu_request: str, memory_request: str, cpu_limit: str, memory_limit: str
) -> dict[str, dict[str, str]]:
    return {
        "requests": {"cpu": cpu_request, "memory": memory_request},
        "limits": {"cpu": cpu_limit, "memory": memory_limit},
    }


def _memory_policy(container_name: str, resources: dict[str, dict[str, str]]) -> ContainerPolicy:
    requests = resources["requests"]
    return ContainerPolicy.memory_requests_only(
        container_name,
        cpu_request=requests["cpu"],
        memory_floor=requests["memory"],
        memory_limit=resources["limits"]["memory"],
    )


def _deployment_annotations_patch(
    name: str, annotations: dict[str, str]
) -> HelmReleaseSpecPostRenderersKustomizePatches:
    # The chart's annotation settings apply to all Deployments or to Pod templates.
    return HelmReleaseSpecPostRenderersKustomizePatches(
        target=HelmReleaseSpecPostRenderersKustomizePatchesTarget(kind="Deployment", name=name),
        patch=yaml_config(
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "annotations": annotations}}
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    operator_resources = _resources(cpu_request="50m", memory_request="128Mi", cpu_limit="250m", memory_limit="256Mi")
    metrics_resources = _resources(cpu_request="50m", memory_request="128Mi", cpu_limit="250m", memory_limit="256Mi")
    webhook_resources = _resources(cpu_request="25m", memory_request="64Mi", cpu_limit="100m", memory_limit="128Mi")
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=VpaProfile(
            Vpa.INITIAL,
            (
                # Keep CPU reservations for these scaling and admission services. The memory floors
                # preserve the startup reservations even when the recommender has sparse samples.
                _memory_policy(_OPERATOR_NAME, operator_resources),
                _memory_policy(METRICS_SERVER_NAME, metrics_resources),
                _memory_policy("keda-admission-webhooks", webhook_resources),
                ContainerPolicy(controlled_values=ControlledValues.REQUESTS_ONLY, mode="Off"),
            ),
        ),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://kedacore.github.io/charts"),
        chart="keda",
        # 2.20.2 reports an empty Forgejo queue as inactive, allowing haku-ci
        # to scale to zero.
        version="2.20.2",
        interval="15m",
        chart_interval="12h",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(
            crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE, remediation=HelmReleaseSpecUpgradeRemediation(retries=3)
        ),
        post_renderers=[
            HelmReleaseSpecPostRenderers(
                kustomize=HelmReleaseSpecPostRenderersKustomize(
                    patches=[
                        _deployment_annotations_patch(
                            "keda-admission-webhooks", {namespaces.VPA_UPDATE_MODE_LABEL: Vpa.AUTO}
                        ),
                        _deployment_annotations_patch(
                            METRICS_SERVER_NAME, {namespaces.VPA_UPDATE_MODE_LABEL: Vpa.AUTO}
                        ),
                        _deployment_annotations_patch(
                            _OPERATOR_NAME,
                            namespaces.initial_request_annotations(
                                ContainerPolicy.cpu_and_memory_requests_only(
                                    _OPERATOR_NAME,
                                    cpu_floor=operator_resources["requests"]["cpu"],
                                    memory_floor=operator_resources["requests"]["memory"],
                                    memory_ceiling=operator_resources["limits"]["memory"],
                                )
                            ),
                        ),
                    ]
                )
            )
        ],
        values={
            # Keep the watch list limited: Helm binds the operator's ClusterRole through
            # namespaced RoleBindings in each watched namespace and the KEDA namespace.
            "watchNamespace": f"{runner.NAMESPACE},buildbuddy-executor",
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            "webhooks": {
                "replicaCount": 2,
                "affinity": node_scheduling.one_per_host({"app": "keda-admission-webhooks"}),
            },
            "metricsServer": {"replicaCount": 2},
            "topologySpreadConstraints": {
                "metricsServer": [node_scheduling.hostname_spread({"app": METRICS_SERVER_NAME})]
            },
            "podDisruptionBudget": {"webhooks": {"minAvailable": 1}, "metricServer": {"minAvailable": 1}},
            "resources": {
                "operator": operator_resources,
                "metricServer": metrics_resources,
                "webhooks": webhook_resources,
            },
        },
    )
    return chart
