"""Flux Kustomizations for the cluster/k8s/agentplane-index slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on


def agentplane_index(
    chart: Chart,
    cnpg: Kustomization,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    local_path_provisioner: Kustomization,
    ollama: Kustomization,
) -> Kustomization:
    name = "agentplane-index"
    return flux_kustomization(
        chart,
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
                flux_kustomization_depends_on(cnpg),
                flux_kustomization_depends_on(external_secrets_config),
                flux_kustomization_depends_on(forgejo_images),
                flux_kustomization_depends_on(local_path_provisioner),
                flux_kustomization_depends_on(ollama),
            ],
        ),
        description=(
            "Complete Agentplane repository-index service: namespace, ESO "
            "credentials, CNPG databases, and both index workers."
        ),
    )
