"""Flux Kustomizations for the cluster/k8s/sshpiper-crds slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def sshpiper_crds() -> dict[str, object]:
    name = "sshpiper-crds"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            path="./plugin/kubernetes",
            # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
            # for the other CRD Kustomizations (agentplane-crds, external-secrets-crds).
            prune=False,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="sshpiper-source", namespace="ducktape-flux"
            ),
        ),
        description="The sshpiper Pipe CRD, sourced from the tag used by the deployed image.",
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/sshpiper-crds/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, sshpiper_crds())
