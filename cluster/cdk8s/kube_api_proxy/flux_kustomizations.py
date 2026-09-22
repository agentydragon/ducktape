"""Flux Kustomizations for the cluster/k8s/kube-api-proxy slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization


def kube_api_proxy(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "kube-api-proxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="kubeapi-proxy", namespace="default"
                ),
                KustomizationSpecHealthChecks(
                    api_version="gateway.networking.k8s.io/v1",
                    kind="HTTPRoute",
                    name="kubeapi-allegedly-works",
                    namespace="default",
                ),
            ],
        ),
    )
