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
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.haku_ci import runner
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "keda"
NAMESPACE = "keda"
OUTPUT_DIR = f"{GENERATED_ROOT}/keda"


def _resources(*, cpu_request: str, memory_request: str, cpu_limit: str, memory_limit: str) -> dict[str, object]:
    return {
        "requests": {"cpu": cpu_request, "memory": memory_request},
        "limits": {"cpu": cpu_limit, "memory": memory_limit},
    }


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND)
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
            # KEDA has cluster-scoped CRDs and admission plumbing, but its operator only
            # watches the one namespace whose runners it is allowed to scale.
            "watchNamespace": runner.NAMESPACE,
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            "resources": {
                "operator": _resources(
                    cpu_request="50m", memory_request="128Mi", cpu_limit="250m", memory_limit="256Mi"
                ),
                "metricServer": _resources(
                    cpu_request="50m", memory_request="128Mi", cpu_limit="250m", memory_limit="256Mi"
                ),
                "webhooks": _resources(
                    cpu_request="25m", memory_request="64Mi", cpu_limit="100m", memory_limit="128Mi"
                ),
            },
        },
    )
    return chart


def keda(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(chart, NAME, directory, timeout="5m", depends_on=[flux_kustomization_depends_on(kyverno)])
