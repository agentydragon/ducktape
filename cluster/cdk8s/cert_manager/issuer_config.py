"""The `cert-manager-issuer-config` ConfigMap: the Flux postBuild substitution source that
selects the Let's Encrypt ClusterIssuer for every Kustomization reading
`${LETSENCRYPT_ISSUER}`."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts

NAME = "cert-manager-issuer-config"
OUTPUT_DIR = "cluster/k8s/cert-manager/issuer-config"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace="flux-system",
            annotations={
                # Flux postBuild substitutions are namespace-local. Keep a reflected copy
                # for migrated consumers until the final flux-system consumer moves.
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "ducktape-flux",
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "ducktape-flux",
            },
        ),
        # Single toggle for Let's Encrypt issuer selection.
        # Change to "letsencrypt-staging" for development (avoids rate limits).
        data={"LETSENCRYPT_ISSUER": "letsencrypt-prod"},
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def cert_manager_issuer_config(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=artifact_source_ref(artifact),
            path=artifact_path(artifact),
            prune=True,
            wait=True,
        ),
    )
