"""Flux Kustomizations for the cluster/k8s/agents slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def plaid_mcp(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    authentik: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "plaid-mcp"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        decryption=SOPS_DECRYPTION,
        # Web OIDC credentials are supplied by an ExternalSecret and required by
        # the pods at startup; wait=True tracks their readiness. Unrelated
        # Authentik Terraform projects must not block app image/config updates.
        # Finance spend policy reconciles independently; its Secret copy and
        # workload become ready once that configuration arrives.
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, authentik, seaweedfs_operator),
    )


def plaid_spend_policy(chart: Chart) -> Kustomization:
    """Reconcile the private Finance policy Secret into its isolated namespace."""
    return flux_kustomization(
        chart,
        "plaid-spend-policy",
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="finance-agent", namespace="ducktape-flux"
        ),
        path="./config/plaid-spend",
        interval="5m",
        retry_interval="1m",
        timeout="2m",
        prune=False,
        target_namespace="finance-spend-config",
        service_account_name="plaid-spend-config-applier",
        description=(
            "Reconciles the private Finance spend-policy Secret into the isolated finance-spend-config namespace. "
            "The impersonated service account can create Secrets of any name there and can get, patch, or update "
            "only the named policy Secret; it cannot delete resources, create namespaces, or manage other resource "
            "kinds. Ducktape owns the namespace and the Secret copy consumed by the app."
        ),
    )


def agent_shared_secrets(chart: Chart, directory: RenderedDirectory, claude_rbac: Kustomization) -> Kustomization:
    name = "agent-shared-secrets"
    return flux_kustomization(
        chart,
        name,
        directory,
        retry_interval=None,
        wait=None,
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(claude_rbac)],
    )
