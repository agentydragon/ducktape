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
from cluster.cdk8s.providers.grafana_operator.grafana import Grafana

NAME = "flux-grafana-secrets"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-grafana-secrets"
_NAMESPACE = "flux-system"
_GRAFANA = "grafana"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Grafana(
        chart,
        "grafana",
        metadata=ApiObjectMetadata(name=_GRAFANA, namespace=_NAMESPACE),
        external=GrafanaSpecExternal(
            url="http://grafana-service.monitoring.svc.cluster.local:3000", tenant_namespace=_NAMESPACE
        ),
        client=GrafanaSpecClient(use_kube_auth=True),
    )
    GrafanaServiceAccount(
        chart,
        "service-account",
        metadata=ApiObjectMetadata(name="flux-notifications", namespace=_NAMESPACE),
        spec=GrafanaServiceAccountSpec(
            instance_name=_GRAFANA,
            role=GrafanaServiceAccountSpecRole.EDITOR,
            tokens=[GrafanaServiceAccountSpecTokens(name="flux-token", secret_name="grafana-flux-token")],
        ),
    )
    return chart


def flux_grafana_secrets(
    chart: Chart, directory: RenderedDirectory, grafana_instance: Kustomization, grafana_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="grafana.integreatly.org/v1beta1",
                kind="GrafanaServiceAccount",
                name="flux-notifications",
                namespace=_NAMESPACE,
            )
        ],
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(grafana_instance, grafana_operator),
    )
