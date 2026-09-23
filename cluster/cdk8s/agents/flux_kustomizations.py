"""Flux Kustomizations for the cluster/k8s/agents slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy, KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def agent_sandbox_controller(chart: Chart, artifact: ArtifactGeneratorSpecArtifacts) -> Kustomization:
    name = "agent-sandbox-controller"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="5m",
        description=(
            "kubernetes-sigs/agent-sandbox v0.5.5 combined release asset "
            "(Sandbox, SandboxTemplate, SandboxClaim, SandboxWarmPool CRDs)."
        ),
    )


def airlock(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, external_secrets_operator: Kustomization
) -> Kustomization:
    name = "airlock"
    return flux_kustomization(
        chart,
        name,
        artifact,
        suspend=False,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
    )


def authentik_jwt_rotation(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, external_secrets_operator: Kustomization
) -> Kustomization:
    name = "authentik-jwt-rotation"
    return flux_kustomization(
        chart,
        name,
        artifact,
        wait=None,
        depends_on=[flux_kustomization_depends_on(external_secrets_operator)],
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="external-secrets.io/v1",
                kind="ExternalSecret",
                name="github-secrets-sync-pat",
                namespace="agents-infra",
            )
        ],
        timeout="2m",
    )


def forgejo_token_rotation(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo_images: Kustomization,
    authentik_jwt_rotation: Kustomization,
    forgejo_claude: Kustomization,
    haku_state: Kustomization,
    forgejo_agentydragon_repos: Kustomization,
) -> Kustomization:
    name = "forgejo-token-rotation"
    return flux_kustomization(
        chart,
        name,
        artifact,
        retry_interval=None,
        wait=None,
        depends_on=flux_kustomization_depends_on_many(
            forgejo_images,
            # owns the agents-infra namespace
            authentik_jwt_rotation,
            forgejo_claude,
            haku_state,
            forgejo_agentydragon_repos,
        ),
        timeout="2m",
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


def haku_openclaw_spike_app(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_secrets_operator: Kustomization,
    seaweedfs_operator: Kustomization,
) -> Kustomization:
    name = "haku-openclaw-spike-app"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, seaweedfs_operator),
        description=(
            "Isolated OpenClaw gateway using Claude Code subscription inference through the Haku credential proxy."
        ),
    )


def plaid_mcp(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    forgejo_images: Kustomization,
    gateway: Kustomization,
    cnpg: Kustomization,
    local_path_provisioner: Kustomization,
    external_secrets_config: Kustomization,
    valkey: Kustomization,
    agent_machine_access_tf: Kustomization,
    reflector: Kustomization,
    monitoring_crds: Kustomization,
) -> Kustomization:
    name = "plaid-mcp"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            forgejo_images,
            gateway,
            cnpg,
            local_path_provisioner,
            external_secrets_config,
            valkey,
            agent_machine_access_tf,
            reflector,
            # ServiceMonitor
            monitoring_crds,
        ),
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
                api_version="apps/v1",
                kind="Deployment",
                name="public-coder-agent-proxy",
                namespace="public-coder-agent",
            ),
            KustomizationSpecHealthChecks(
                api_version="apps/v1",
                kind="Deployment",
                name="public-coder-agent-sshpiper",
                namespace="public-coder-agent",
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


def public_coder_agent_devbox(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    kubevirt: Kustomization,
    forgejo_images: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    agent_shared_secrets: Kustomization,
    public_coder_agent_app_kustomization: Kustomization,
) -> Kustomization:
    name = "public-coder-agent-devbox"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="30m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            kubevirt,
            forgejo_images,
            external_creds,
            external_secrets_config,
            agent_shared_secrets,
            public_coder_agent_app_kustomization,
        ),
        description=(
            "KubeVirt build/test devbox for public-coder-agent "
            "(Bazel/BuildBuddy/direnv), with an ephemeral containerDisk root "
            "apart from the sshd host key and SSH access through ../sshpiper."
        ),
    )


def agent_shared_secrets(
    chart: Chart, artifact: ArtifactGeneratorSpecArtifacts, claude_rbac: Kustomization
) -> Kustomization:
    name = "agent-shared-secrets"
    return flux_kustomization(
        chart,
        name,
        artifact,
        retry_interval=None,
        wait=None,
        timeout="5m",
        depends_on=[flux_kustomization_depends_on(claude_rbac)],
        decryption=SOPS_DECRYPTION,
    )


def tana_mcp(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
    valkey: Kustomization,
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
            external_creds,
            external_secrets_config,
            valkey,
            # ServiceMonitor + PrometheusRule
            monitoring_crds,
        ),
    )
