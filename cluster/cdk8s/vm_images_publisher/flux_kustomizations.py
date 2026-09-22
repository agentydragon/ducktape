"""Flux Kustomizations for the cluster/k8s/vm-images-publisher slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpec, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.artifact_generators import artifact_path, artifact_source_ref
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on


def vm_images_publisher(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, seaweedfs_cluster: Kustomization
) -> Kustomization:
    name = "vm-images-publisher"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path=artifact_path(artifact),
            prune=True,
            # attic-reader-netrc.sops.yaml is SOPS-encrypted; without this, Flux applies
            # the ciphertext literally and the publisher's attic auth (netrc) is garbage.
            source_ref=artifact_source_ref(artifact),
            decryption=SOPS_DECRYPTION,
            depends_on=[flux_kustomization_depends_on(seaweedfs_cluster)],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="vm-images",
                    namespace="vm-images-publisher",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="vm-images-ci-writer",
                    namespace="vm-images-publisher",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="vm-images-cdi-reader",
                    namespace="vm-images-publisher",
                ),
                KustomizationSpecHealthChecks(
                    api_version="batch/v1", kind="CronJob", name="vm-images-publisher", namespace="vm-images-publisher"
                ),
            ],
        ),
    )
