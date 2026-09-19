"""Flux Kustomizations for the cluster/k8s/activitywatch slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def activitywatch() -> dict[str, object]:
    name = "activitywatch"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            # Revived 2026-08-26: the central aw-server is fed by the repo-owned
            # instance-to-instance importer (@ducktape_activitywatch//importer) over a
            # bearer-gated write route, replacing aw-sync. The cluster Syncthing receiver,
            # importer cronjob, and desktop Syncthing transport are gone.
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/activitywatch",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            # Only local-path-proxmox (activitywatch-data) is used now that Syncthing and its
            # seaweedfs sync-inbox are gone -- so no seaweedfs-csi dependency, which otherwise
            # blocks the revive whenever seaweedfs-csi is degraded.
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/activitywatch/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, activitywatch())
