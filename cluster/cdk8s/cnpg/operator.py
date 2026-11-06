"""The CloudNativePG operator and its Barman Cloud backup plugin: the `cnpg-system`
Namespace, the shared HelmRepository and both HelmReleases, which install their own CRDs.

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
    HelmReleaseSpecUpgradeRemediation,
)

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "cnpg"
NAMESPACE = "cnpg-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/cnpg"
_BARMAN_CLOUD = "plugin-barman-cloud"
# The operator backs two failurePolicy: Fail webhooks (Cluster, Backup, ScheduledBackup),
# so while it is down those writes are rejected outright — and it is the operator
# reconciling every Postgres cluster here. Same treatment as the other blocking-webhook
# backends.
#
# This is the operator Deployment only. Database placement is unaffected: each Cluster
# carries its own affinity (docs/cnpg_conventions.md), and nothing here would put Postgres
# on a control-plane node.
_CRITICAL_VALUES: dict[str, object] = {
    "priorityClassName": "system-cluster-critical",
    "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
}


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.INITIAL)
    repository = https_helm_repository(chart, NAME, "flux-system", url="https://cloudnative-pg.github.io/charts")
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=repository,
        chart="cloudnative-pg",
        version="0.29.0",
        interval="30m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE),
        values=_CRITICAL_VALUES,
    )
    helm_release(
        chart,
        _BARMAN_CLOUD,
        NAMESPACE,
        repository=repository,
        chart=_BARMAN_CLOUD,
        version="0.8.0",
        interval="30m",
        install=HelmReleaseSpecInstall(
            crds=HelmReleaseSpecInstallCrds.CREATE_REPLACE, remediation=HelmReleaseSpecInstallRemediation(retries=3)
        ),
        upgrade=HelmReleaseSpecUpgrade(
            crds=HelmReleaseSpecUpgradeCrds.CREATE_REPLACE, remediation=HelmReleaseSpecUpgradeRemediation(retries=3)
        ),
        values=_CRITICAL_VALUES,
        description="CloudNativePG Barman Cloud plugin for physical backups and WAL archiving.",
    )
    return chart


def cnpg(chart: Chart, directory: RenderedDirectory, cert_manager: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="10m",
        # The plugin-barman-cloud chart renders a cert-manager Issuer and Certificates.
        depends_on=[flux_kustomization_depends_on(cert_manager)],
    )
