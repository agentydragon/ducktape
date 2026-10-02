"""Flux Kustomizations for the cluster/k8s/agents slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def airlock(chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization) -> Kustomization:
    name = "airlock"
    return flux_kustomization(
        chart,
        name,
        directory,
        suspend=False,
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
    )


def haku_egress_proxy(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "haku-egress-proxy"
    return flux_kustomization(
        chart,
        name,
        artifact,
        retry_interval=None,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(cert_manager, cert_manager_trust, external_secrets_operator),
        decryption=SOPS_DECRYPTION,
    )


def plaid_mcp(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    authentik: Kustomization,
    authentik_tf: Kustomization,
) -> Kustomization:
    name = "plaid-mcp"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(cnpg, external_secrets_operator, authentik, authentik_tf),
    )


def public_coder_agent_app(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    external_secrets_operator: Kustomization,
    sshpiper_crds: Kustomization,
) -> Kustomization:
    name = "public-coder-agent-app"
    return flux_kustomization(
        chart,
        name,
        artifact,
        wait=None,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        decryption=SOPS_DECRYPTION,
        timeout="5m",
        # Admission prerequisites for Certificate, Bundle, ExternalSecret and Pipe resources.
        # Runtime credentials and services can reconcile after the namespace and workloads land.
        depends_on=flux_kustomization_depends_on_many(
            cert_manager, cert_manager_trust, external_secrets_operator, sshpiper_crds
        ),
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="public-coder-agent", namespace="public-coder-agent"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="proxy", namespace="public-coder-agent"
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1", kind="Deployment", name="sshpiper", namespace="public-coder-agent"
            ),
            KustomizationSpecHealthChecks(
                api_version="cert-manager.io/v1",
                kind="Certificate",
                name="public-coder-agent-proxy-root-ca",
                namespace="public-coder-agent",
            ),
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name="brave-search-api-key",
                namespace="public-coder-agent",
            ),
        ],
        description=(
            "OpenClaw coder agent namespace, application, Iron proxy and SSH bastion; "
            "devbox and backups reconcile separately."
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


def tana_mcp(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    mcp_oauth_state: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "tana-mcp"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator,
            mcp_oauth_state,
            # ServiceMonitor + PrometheusRule
            monitoring_crds,
        ),
    )
