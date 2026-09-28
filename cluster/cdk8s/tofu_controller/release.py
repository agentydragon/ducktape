"""tofu-controller: its HelmRepository and the HelmRelease, which also installs the
`Terraform` CRD.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import App, Chart
from flux_helm.io.fluxcd.toolkit.helm import (
    HelmReleaseSpecInstall,
    HelmReleaseSpecInstallCrds,
    HelmReleaseSpecInstallRemediation,
    HelmReleaseSpecUpgrade,
    HelmReleaseSpecUpgradeCrds,
)

from cluster.cdk8s import flux, terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import helm_release, helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "tofu-controller"
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/tofu-controller"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=helm_repository(chart, NAME, NAMESPACE, url="https://flux-iac.github.io/tofu-controller"),
        chart="tofu-controller",
        version="0.16.5",
        interval="15m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
        values={
            # Gitops Terraform CRs read the ducktape GitRepository in the Flux namespace.
            "allowCrossNamespaceRefs": terraform.NAMESPACE != flux.NAMESPACE,
            "runner": {
                "grpc": {"maxMessageSize": 50},  # MB, default 4 — defense against repo growth
                "serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": ""}},  # Not needed for on-prem
            },
            # Security context for Terraform runner pods
            "podSecurityContext": {"runAsNonRoot": True, "runAsUser": 65532, "fsGroup": 65532},
            # Resource limits for runner pods
            "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}, "requests": {"cpu": "100m", "memory": "128Mi"}},
            "logLevel": "info",
        },
    )
    return chart


def tofu_controller(chart: Chart, directory: RenderedDirectory, kyverno: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        interval="10m0s",
        timeout="10m0s",
        depends_on=flux_kustomization_depends_on_many(kyverno),
    )
