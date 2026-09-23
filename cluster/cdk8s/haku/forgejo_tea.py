"""haku-forgejo-tea: the SOPS-encrypted Forgejo API token Reflector mirrors into haku-ci's KEDA
scaler. The Secret stays hand-written; this writes the directory's Kustomization."""

from __future__ import annotations

from pathlib import Path

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization, KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    flux_kustomization,
    flux_kustomization_depends_on,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_yaml

NAME = "haku-forgejo-tea"
OUTPUT_DIR = "cluster/k8s/haku/forgejo-tea"


def haku_forgejo_tea(
    flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, root: Path, haku_rbac: Kustomization
) -> Kustomization:
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=[f"{NAME}.sops.yaml"]))
    return flux_kustomization(
        flux_chart,
        NAME,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path=artifact_path(artifact),
            prune=True,
            wait=True,
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            depends_on=[
                # haku-sandbox ns the secret lives in
                flux_kustomization_depends_on(haku_rbac)
            ],
        ),
        description=(
            "Forgejo API token Reflector mirrors into haku-ci's KEDA scaler. "
            "Split out of haku/managed-agent so haku-ci doesn't depend on the "
            "(parked) worker."
        ),
    )
