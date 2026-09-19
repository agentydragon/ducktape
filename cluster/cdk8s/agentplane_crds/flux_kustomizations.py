"""Flux Kustomizations for the cluster/k8s/agentplane-crds slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def agentplane_crds() -> dict[str, object]:
    name = "agentplane-crds"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            path="./cluster/k8s/agentplane-crds",
            # Pruning a CRD deletes every instance with it; removing one is a deliberate manual step, as
            # for the other CRD Kustomizations (external-secrets-crds, snapshot-controller-crds).
            prune=False,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
        description=(
            "Agentplane's own CRDs (EgressPolicy, EgressBinding, "
            "EgressCredential, ActionPolicySet, ActionPolicyBinding), "
            "cluster-scoped and shared by every Agentplane namespace; the "
            "environment seeds, the egress proxy and the Action Service depend on "
            "this."
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/agentplane-crds/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agentplane_crds())
