"""Shared helpers for writing generated cdk8s manifests into cluster/k8s."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from cdk8s import App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
)

from cluster.cdk8s.flux import KustomizeKustomization
from cluster.cdk8s.metadata import metadata

CNPG_DATABASE_READY = (
    "has(status.applied) && status.applied && "
    "has(status.observedGeneration) && status.observedGeneration == metadata.generation"
)


def write_yaml(path: Path, manifest: KustomizeKustomization) -> None:
    path.write_text(Yaml.format_objects([manifest.model_dump(by_alias=True, exclude_none=True)]))


def config_map_chart(app: App, *, chart_name: str, configmap_name: str, namespace: str, data: dict[str, str]) -> Chart:
    chart = Chart(app, chart_name, disable_resource_name_hashes=True)
    ConfigMap(chart, "config", metadata=metadata(configmap_name, namespace), data=data)
    return chart


def write_charts(root: Path, app_dir: str, *chart_builders: Callable[[App], Chart]) -> None:
    """Synthesize charts into one Kustomization directory."""
    out_dir = root / app_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    for build in chart_builders:
        build(app)
    app.synth()


def sops_decryption(resources: Sequence[str]) -> KustomizationSpecDecryption | None:
    """Return the Flux decryption block when a directory lists an encrypted Secret."""
    if not any(resource.endswith(".sops.yaml") for resource in resources):
        return None
    return KustomizationSpecDecryption(
        provider=KustomizationSpecDecryptionProvider.SOPS,
        secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
    )
