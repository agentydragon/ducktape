"""Flux Kustomizations for the cluster/k8s/ollama slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many


def ollama(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    claude_rbac: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    name = "ollama"
    return flux_kustomization(
        chart,
        name,
        artifact,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="10m",
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name="ollama-direct-token",
                namespace="ollama",
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            # ExternalSecret CRD and ESO's failurePolicy: Fail webhook
            external_secrets_operator,
            claude_rbac,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployment, HTTPRoute and Namespace.
            kyverno,
        ),
    )
