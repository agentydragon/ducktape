"""The `cert-manager-issuer-config` ConfigMap: the Flux postBuild substitution source for
Kustomizations whose applied artifact may still read `${LETSENCRYPT_ISSUER}`."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.flux import CERT_MANAGER_ISSUER_CONFIG, NAMESPACE, Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

NAME = CERT_MANAGER_ISSUER_CONFIG
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
                # Flux postBuild substitutions are namespace-local, and every consumer
                # Kustomization lives in the shared Flux namespace.
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": NAMESPACE,
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": NAMESPACE,
            },
        ),
        # CLEANUP(added 2026-09-27): delete this unit once #8160 has applied on the cluster.
        data={"LETSENCRYPT_ISSUER": LETSENCRYPT_ISSUER},
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def cert_manager_issuer_config(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    return flux_kustomization(chart, NAME, artifact, timeout="5m")
