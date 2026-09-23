"""The `seaweedfs-credentials` namespace holding the canonical, externally managed SeaweedFS
S3 credential Secrets (hand-written `*.sops.yaml` beside the output), and the grants that
let only `S3Credentials` in the operator's namespace read each one.

The Secrets live outside the operator's namespace so the operator consumes them read-only;
`public_s3` registers them with native IAM.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.seaweedfs import s3

NAMESPACE = "seaweedfs-credentials"
OUTPUT_DIR = "cluster/k8s/seaweedfs/external-credentials"
CLAUDE_READER_SECRET = "claude-reader-s3-credentials"
DRIVEFS_ARTIFACTS_SECRET = "drivefs-artifacts-s3-credentials"
_CHART = "external-credentials"
_SECRET_FILES = ("claude-reader-credentials.sops.yaml", "drivefs-artifacts-credentials.sops.yaml")


def chart(app: App) -> Chart:
    chart = Chart(app, _CHART, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAMESPACE,
            labels={"name": NAMESPACE},
            annotations={"description": "Externally managed SeaweedFS S3 credential source Secrets."},
        ),
    )
    for secret in (CLAUDE_READER_SECRET, DRIVEFS_ARTIFACTS_SECRET):
        s3.secret_grant(chart, secret=secret, namespace=NAMESPACE)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[f"{_CHART}.k8s.yaml", *_SECRET_FILES]),
    )


def seaweedfs_external_credentials(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    seaweedfs_secrets: Kustomization,
    seaweedfs_cluster: Kustomization,
) -> Kustomization:
    name = "seaweedfs-external-credentials"
    return flux_kustomization(
        chart,
        name,
        artifact,
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(seaweedfs_secrets, seaweedfs_cluster),
        timeout="5m",
        description="Externally managed SeaweedFS S3 credential source Secrets and grants.",
    )
