"""KEDA, the autoscaler that scales haku-ci's runners, in its own namespace."""

from __future__ import annotations

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
    HelmReleaseSpecUpgradeRemediation,
)

from cluster.cdk8s import namespaces
from cluster.cdk8s.haku_ci import runner
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa, VpaProfile
from cluster.cdk8s.vpa_policy import ContainerPolicy, ControlledValues

NAME = "keda"
NAMESPACE = "keda"
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
        memory_ceiling=resources["limits"]["memory"],
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
                _memory_policy("keda-operator", operator_resources),
                _memory_policy("keda-operator-metrics-apiserver", metrics_resources),
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
        values={
            # Keep the watch list limited: Helm binds the operator's ClusterRole through
            # namespaced RoleBindings in each watched namespace and the KEDA namespace.
            "watchNamespace": f"{runner.NAMESPACE},buildbuddy-executor",
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            "resources": {
                "operator": operator_resources,
                "metricServer": metrics_resources,
                "webhooks": webhook_resources,
            },
        },
    )
    return chart
