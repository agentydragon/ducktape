"""Flux's Grafana service-account token: a `Grafana` CR in flux-system pointing at the
monitoring instance, and the `GrafanaServiceAccount` whose token Secret Flux's
notification provider reads."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from grafana_grafana_crds.org.integreatly.grafana import Grafana, GrafanaSpec, GrafanaSpecClient, GrafanaSpecExternal
from grafana_grafanaserviceaccount_crds.org.integreatly.grafana import (
    GrafanaServiceAccount,
    GrafanaServiceAccountSpec,
    GrafanaServiceAccountSpecRole,
    GrafanaServiceAccountSpecTokens,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "flux-grafana-secrets"
OUTPUT_DIR = f"{GENERATED_ROOT}/flux-grafana-secrets"
_NAMESPACE = "flux-system"
_GRAFANA = "grafana"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    Grafana(
        chart,
        "grafana",
        metadata=metadata(_GRAFANA, _NAMESPACE),
        spec=GrafanaSpec(
            external=GrafanaSpecExternal(
                url="http://grafana-service.monitoring.svc.cluster.local:3000", tenant_namespace=_NAMESPACE
            ),
            client=GrafanaSpecClient(use_kube_auth=True),
        ),
    )
    GrafanaServiceAccount(
        chart,
        "service-account",
        metadata=metadata("flux-notifications", _NAMESPACE),
        spec=GrafanaServiceAccountSpec(
            instance_name=_GRAFANA,
            role=GrafanaServiceAccountSpecRole.EDITOR,
            tokens=[GrafanaServiceAccountSpecTokens(name="flux-token", secret_name="grafana-flux-token")],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def flux_grafana_secrets(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    grafana_instance: Kustomization,
    grafana_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
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
