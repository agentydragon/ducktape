"""Flux's Grafana service-account token: a `Grafana` CR in flux-system pointing at the
monitoring instance, and the `GrafanaServiceAccount` whose token Secret Flux's
notification provider reads."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from grafana_grafana_crds.org.integreatly.grafana import GrafanaSpecClient, GrafanaSpecExternal
from grafana_grafanaserviceaccount_crds.org.integreatly.grafana import (
    GrafanaServiceAccount,
    GrafanaServiceAccountSpec,
    GrafanaServiceAccountSpecRole,
    GrafanaServiceAccountSpecTokens,
)

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.monitoring import grafana_instance
from cluster.cdk8s.providers.grafana_operator.grafana import Grafana
from cluster.cdk8s.secret_ref import SecretRef

NAME = "flux-grafana-secrets"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-grafana-secrets"
_NAMESPACE = "flux-system"
_GRAFANA = "grafana"
_ACCOUNT = "flux-notifications"
TOKEN = SecretRef(namespace=_NAMESPACE, name="grafana-flux-token")


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Grafana(
        chart,
        "grafana",
        metadata=ApiObjectMetadata(name=_GRAFANA, namespace=_NAMESPACE),
        external=GrafanaSpecExternal(
            url=f"http://{grafana_instance.SERVICE.fqdn}:{grafana_instance.SERVICE.port.number}",
            tenant_namespace=_NAMESPACE,
        ),
        client=GrafanaSpecClient(use_kube_auth=True),
    )
    GrafanaServiceAccount(
        chart,
        "service-account",
        metadata=ApiObjectMetadata(name=_ACCOUNT, namespace=_NAMESPACE),
        spec=GrafanaServiceAccountSpec(
            instance_name=_GRAFANA,
            role=GrafanaServiceAccountSpecRole.EDITOR,
            tokens=[GrafanaServiceAccountSpecTokens(name="flux-token", secret_name=TOKEN.name)],
        ),
    )
    return chart


def flux_annotations(chart: Chart, directory: RenderedDirectory, grafana_operator: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="grafana.integreatly.org/v1beta1",
                kind="GrafanaServiceAccount",
                name=_ACCOUNT,
                namespace=_NAMESPACE,
            )
        ],
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(grafana_operator),
    )
