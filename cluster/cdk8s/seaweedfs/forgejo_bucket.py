"""The cluster-global `forgejo` S3Identity. The Forgejo Kustomization owns the Bucket and the
namespaced S3Credentials that reference it."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from seaweed_s3identity_crds.com.seaweedfs.seaweed import (
    S3Identity,
    S3IdentitySpec,
    S3IdentitySpecReclaimPolicy,
    S3IdentitySpecSeaweedRef,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.seaweedfs import cluster, namespace

NAME = "forgejo"
OUTPUT_DIR = "cluster/k8s/seaweedfs/forgejo-bucket"
_CHART = "forgejo-bucket"


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


def seaweedfs_forgejo_bucket(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "seaweedfs-forgejo-bucket"
    return flux_kustomization(
        chart,
        name,
        # The Bucket and S3Credentials moved to the Forgejo Kustomization and were
        # live-verified there. Keep this small Kustomization for the cluster-global
        # S3Identity that the namespaced S3Credentials references.
        artifact,
        depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
        timeout="5m",
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="seaweed.seaweedfs.com/v1", kind="S3Identity", name=NAME, namespace=namespace.NAME
            )
        ],
    )
