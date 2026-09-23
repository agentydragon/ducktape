"""haku-forgejo-tea: the SOPS-encrypted Forgejo API token Reflector mirrors into haku-ci's KEDA
scaler. The Secret stays hand-written; this builds the directory's Flux Kustomization."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import Kustomization, KustomizationSpec
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, flux_kustomization, flux_kustomization_depends_on

NAME = "haku-forgejo-tea"
OUTPUT_DIR = "cluster/k8s/haku/forgejo-tea"


def haku_forgejo_tea(
    flux_chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, haku_rbac: Kustomization
) -> Kustomization:
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
