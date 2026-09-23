"""The cluster-global `public-coder-agent-backups` S3Identity that public-coder-agent's backup
credentials reference."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import cluster, namespace

NAME = "public-coder-agent-backups"
OUTPUT_DIR = "cluster/k8s/seaweedfs/public-coder-agent-backups-bucket"
_CHART = "public-coder-agent-backups-bucket"


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    S3Identity(
        chart,
        "identity",
        metadata=metadata(NAME, namespace.NAME),
        spec=S3IdentitySpec(
            seaweed_ref=S3IdentitySpecSeaweedRef(name=cluster.NAME), reclaim_policy=S3IdentitySpecReclaimPolicy.RETAIN
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(root / OUTPUT_DIR / "kustomization.yaml", kustomize_kustomization(resources=[f"{_CHART}.k8s.yaml"]))


def seaweedfs_public_coder_agent_backups_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-public-coder-agent-backups-bucket"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path=artifact_path(artifact),
            prune=True,
            source_ref=artifact_source_ref(artifact),
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            timeout="5m",
        ),
    )
