"""Flux Kustomizations for the cluster/k8s/agentplane-index slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def agentplane_index() -> dict[str, object]:
    name = "agentplane-index"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/agentplane-index",
            prune=True,
            # haku-state's Terraform Kustomization depends on this aggregate to create
            # the target Namespace, then reflects haku-forgejo-git into it. Waiting for
            # the haku-state Deployment here would deadlock that bootstrap: the Pod
            # needs the Secret created by the dependency.
            wait=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="agentplane-index"),
                KustomizationSpecHealthChecks(
                    api_version="postgresql.cnpg.io/v1",
                    kind="Cluster",
                    name="agentplane-index-db",
                    namespace="agentplane-index",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="ducktape", namespace="agentplane-index"
                ),
            ],
            health_check_exprs=[
                KustomizationSpecHealthCheckExprs(
                    api_version="postgresql.cnpg.io/v1",
                    kind="Database",
                    current=(
                        "has(status.applied) && status.applied && "
                        "has(status.observedGeneration) && status.observedGeneration == "
                        "metadata.generation && has(status.extensions) && "
                        "status.extensions.exists(e, e.name == 'vector' && e.applied)"
                    ),
                )
            ],
            depends_on=[
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="ollama", namespace="ducktape-flux"),
            ],
        ),
        description=(
            "Complete Agentplane repository-index service: namespace, ESO "
            "credentials, CNPG databases, and both index workers."
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/agentplane-index/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agentplane_index())
