"""The budget Namespace, its copy of the ledger's git credentials, and the `budget-namespace`
Flux Kustomization owning both."""

from __future__ import annotations

from cdk8s import App, Chart

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

OUTPUT_DIR = f"{GENERATED_ROOT}/forgejo/budget-namespace"
_NAMESPACE = "budget"


def chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=_NAMESPACE,
        vpa=Vpa.AUTO,
        annotations={
            # The suspended parked/budget Kustomization may still have this namespace in
            # its inventory. Keep that stale inventory from pruning the active namespace.
            "kustomize.toolkit.fluxcd.io/prune": "disabled"
        },
    )
    return chart


def git_credentials_chart(app: App) -> Chart:
    """tf/gitops/budget-ledger's service-user credentials, for the parked Fava
    (cluster/parked/budget) and, mirrored into augur, gaffer-private's ledger exporter."""
    chart = Chart(app, "git-credentials", disable_resource_name_hashes=True)
    secret_copy.secret_copy(
        chart, "budget-ledger-git-creds", reader=secret_copy.reader(chart, _NAMESPACE), mirror_namespaces=["augur"]
    )
    return chart


def budget_namespace(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization
) -> Kustomization:
    name = "budget-namespace"
    return flux_kustomization(
        chart,
        name,
        directory,
        retry_interval=None,
        wait=None,
        interval="1h",
        prune=False,
        timeout="1m",
        depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
    )
