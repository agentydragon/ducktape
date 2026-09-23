"""The `cert-manager-issuer-config` ConfigMap: the Flux postBuild substitution source that
selects the Let's Encrypt ClusterIssuer for every Kustomization reading
`${LETSENCRYPT_ISSUER}`."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = "cert-manager-issuer-config"
OUTPUT_DIR = f"{GENERATED_ROOT}/cert-manager/issuer-config"


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
    return flux_kustomization(chart, NAME, artifact, timeout="5m")
