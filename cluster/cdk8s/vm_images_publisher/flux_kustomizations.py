"""Flux Kustomizations for the cluster/k8s/vm-images-publisher slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def vm_images_publisher() -> dict[str, object]:
    name = "vm-images-publisher"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path="./cluster/k8s/vm-images-publisher",
            prune=True,
            # attic-reader-netrc.sops.yaml is SOPS-encrypted; without this, Flux applies
            # the ciphertext literally and the publisher's attic auth (netrc) is garbage.
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux")],
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


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/vm-images-publisher/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, vm_images_publisher())
