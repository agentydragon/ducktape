"""Flux Kustomizations for the cluster/k8s/agents slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on


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
