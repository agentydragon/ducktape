"""Flux Kustomizations for the cluster/k8s/agentplane-egress-credentials slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def agentplane_egress_credentials_namespace() -> dict[str, object]:
    name = "agentplane-egress-credentials-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            timeout="2m",
            path="./cluster/k8s/agentplane-egress-credentials/namespace",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
        ),
        description="The namespace holding the credentials the Agentplane egress proxy substitutes.",
    )


def agentplane_egress_credentials() -> dict[str, object]:
    name = "agentplane-egress-credentials"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="2m",
            path="./cluster/k8s/agentplane-egress-credentials/secrets",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="agentplane-egress-credentials-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="external-creds",  # the source-side grant on the agentydragon-agent PAT
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="external-secrets-config",  # the ClusterSecretStore the PAT is read through
                    namespace="ducktape-flux",
                ),
            ],
        ),
        description=(
            "The credentials the Agentplane egress proxy substitutes (the "
            "agentydragon-agent GitHub PAT via ESO) and the proxy "
            "ServiceAccount's read grant on them."
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/agentplane-egress-credentials/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agentplane_egress_credentials_namespace())
    path = root / "cluster/k8s/agentplane-egress-credentials/secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agentplane_egress_credentials())
