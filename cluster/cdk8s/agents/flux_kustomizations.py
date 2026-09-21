"""Flux Kustomizations for the cluster/k8s/agents slice."""

from __future__ import annotations

from cdk8s import Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on,
    flux_kustomization_depends_on_many,
)


def claude_rbac(chart: Chart, kyverno_policies: Kustomization) -> Kustomization:
    # TODO: migrate this live Flux object name to agent-rbac-base in a staged
    # change. Renaming it directly would delete the old Kustomization and may prune
    # its inventory before the replacement owns the same RBAC resources.
    name = "claude-rbac"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/agent-rbac-base",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[flux_kustomization_depends_on(kyverno_policies)],
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="claude-sandbox")],
        ),
        description=(
            "Lightweight base for agent RBAC. Claude sandbox namespace + shared "
            "ClusterRoles. Must not depend on service or database kustomizations."
        ),
    )


def agent_sandbox_controller(chart: Chart) -> Kustomization:
    name = "agent-sandbox-controller"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            path="./cluster/k8s/agents/agent-sandbox/controller",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="agent-sandbox-controller",
                    namespace="agent-sandbox-system",
                )
            ],
        ),
        description=(
            "kubernetes-sigs/agent-sandbox v0.5.5 combined release asset "
            "(Sandbox, SandboxTemplate, SandboxClaim, SandboxWarmPool CRDs)."
        ),
    )


def agent_workspaces_app(
    chart: Chart,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
    agent_sandbox_controller: Kustomization,
    litellm_keys_tf: Kustomization,
    kyverno_policies: Kustomization,
) -> Kustomization:
    name = "agent-workspaces-app"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            path="./cluster/k8s/agents/agent-sandbox/workspaces",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="agent-workspaces")],
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                forgejo_images,
                # CRDs + controller
                agent_sandbox_controller,
                # mints + reflects the Codex workspace key
                litellm_keys_tf,
                # cleanup-controller ClusterRole the janitor needs
                kyverno_policies,
            ),
        ),
        description=(
            "Disposable agent workspace template + warm pool in agent-workspaces. "
            "See agents/agent-sandbox/README.md for usage."
        ),
    )


def airlock(chart: Chart, external_secrets_config: Kustomization) -> Kustomization:
    name = "airlock"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/agents/airlock",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="airlock", namespace="airlock"
                )
            ],
            depends_on=[flux_kustomization_depends_on(external_secrets_config)],
        ),
    )


def alloy_otlp_bearer(
    chart: Chart, external_secrets_config: Kustomization, claude_rbac: Kustomization, haku_rbac: Kustomization
) -> Kustomization:
    name = "alloy-otlp-bearer"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/alloy-otlp-bearer",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                # ClusterSecretStore + CRDs
                external_secrets_config,
                # claude-sandbox namespace
                claude_rbac,
                # haku-sandbox namespace
                haku_rbac,
            ),
            timeout="2m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def authentik_jwt_rotation(chart: Chart, external_secrets_config: Kustomization) -> Kustomization:
    name = "authentik-jwt-rotation"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/agents/authentik-jwt-rotation",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[flux_kustomization_depends_on(external_secrets_config)],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="github-secrets-sync-pat",
                    namespace="agents-infra",
                )
            ],
            timeout="2m",
        ),
    )


def claude_sandbox_secrets(
    chart: Chart, claude_rbac: Kustomization, external_secrets_config: Kustomization
) -> Kustomization:
    name = "claude-sandbox-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/agents/claude-sandbox-secrets",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(claude_rbac, external_secrets_config),
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="openclaw-telegram-bot-token",
                    namespace="claude-sandbox",
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="buildbuddy-api-key",
                    namespace="claude-sandbox",
                ),
            ],
        ),
    )


def forgejo_token_rotation(
    chart: Chart,
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
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/forgejo-token-rotation",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(
                forgejo_images,
                # owns the agents-infra namespace
                authentik_jwt_rotation,
                forgejo_claude,
                haku_state,
                forgejo_agentydragon_repos,
            ),
            timeout="2m",
        ),
    )


def haku_egress_proxy(
    chart: Chart,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    name = "haku-egress-proxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/haku-egress-proxy",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=flux_kustomization_depends_on_many(cert_manager, cert_manager_trust, external_secrets_operator),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def haku_openclaw_spike_app(
    chart: Chart, external_secrets_operator: Kustomization, seaweedfs_operator: Kustomization
) -> Kustomization:
    name = "haku-openclaw-spike-app"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/agents/haku-openclaw-spike/app",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=flux_kustomization_depends_on_many(external_secrets_operator, seaweedfs_operator),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="haku-openclaw-spike-backups",
                    namespace="haku-openclaw-spike",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="haku-openclaw-spike-backups",
                    namespace="haku-openclaw-spike",
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="haku-openclaw-spike",
                    namespace="haku-openclaw-spike",
                ),
            ],
        ),
        description=(
            "Isolated OpenClaw gateway using Claude Code subscription inference through the Haku credential proxy."
        ),
    )


def haku_openclaw_spike_backup(
    chart: Chart, external_secrets_operator: Kustomization, volsync: Kustomization
) -> Kustomization:
    name = "haku-openclaw-spike-backup"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            wait=True,
            path="./cluster/k8s/agents/haku-openclaw-spike/backup",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(
                # Backup/S3 wiring must converge even when the OpenClaw Deployment is down.
                # The Bucket and S3Credentials remain app-owned, but their readiness is
                # retried by the ExternalSecret rather than coupling this Kustomization to
                # the app Deployment health check.
                external_secrets_operator,
                volsync,
            ),
        ),
        description=(
            "Restic/VolSync backup of the Haku OpenClaw spike state to its "
            "dedicated private SeaweedFS S3 bucket, plus the one-shot restore "
            "into the optiplex worker PVC that migrates the state off the control "
            "plane."
        ),
    )


def kubectl_passthrough_mcp(chart: Chart) -> Kustomization:
    name = "kubectl-passthrough-mcp"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            suspend=False,
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/agents/kubectl-passthrough-mcp/app",
            prune=True,
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="kubectl-passthrough-mcp",
                    namespace="kubectl-passthrough-mcp",
                )
            ],
        ),
    )


def loki_read_proxy(chart: Chart, external_secrets_config: Kustomization) -> Kustomization:
    name = "loki-read-proxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            path="./cluster/k8s/agents/loki-read-proxy",
            prune=True,
            wait=True,
            depends_on=[flux_kustomization_depends_on(external_secrets_config)],
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="loki-read-proxy", namespace="loki-read-proxy"
                )
            ],
        ),
        description=(
            "Read-only namespace-filtering Loki query proxy so Haku can read logs "
            "for allowlisted namespaces without touching Loki "
            "(auth_enabled:false) directly."
        ),
    )


def agent_machine_access_tf(
    chart: Chart, tofu_controller: Kustomization, tofu_state_db: Kustomization, authentik: Kustomization
) -> Kustomization:
    name = "agent-machine-access-tf"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            path="./cluster/k8s/agents/machine-access-tf",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="infra.contrib.fluxcd.io/v1alpha2",
                    kind="Terraform",
                    name="agent-machine-access",
                    namespace="flux-system",
                )
            ],
            timeout="10m",
            depends_on=flux_kustomization_depends_on_many(tofu_controller, tofu_state_db, authentik),
        ),
    )


def agents_mitmproxy(chart: Chart, cert_manager_trust: Kustomization) -> Kustomization:
    name = "agents-mitmproxy"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/mitmproxy",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
            ),
            timeout="5m",
            # Installs Bundle CRDs and transitively the Certificate CRDs.
            depends_on=[flux_kustomization_depends_on(cert_manager_trust)],
        ),
    )


def plaid_mcp(
    chart: Chart,
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
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="10m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/agents/plaid-mcp",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="postgresql.cnpg.io/v1", kind="Cluster", name="plaid-mcp-db", namespace="plaid-mcp"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="plaid-mcp", namespace="plaid-mcp"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="plaid-db-mcp", namespace="plaid-mcp"
                ),
            ],
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
        ),
    )


def public_coder_agent_app(
    chart: Chart,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    external_secrets_operator: Kustomization,
    sshpiper_crds: Kustomization,
) -> Kustomization:
    name = "public-coder-agent-app"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/public-coder-agent/app",
            prune=True,
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            timeout="5m",
            retry_interval="1m",
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
        ),
        description=(
            "OpenClaw coder agent namespace, application, Iron proxy and SSH bastion; "
            "devbox and backups reconcile separately."
        ),
    )


def public_coder_agent_backup(
    chart: Chart,
    seaweedfs_public_coder_agent_backups_bucket: Kustomization,
    external_secrets_config: Kustomization,
    volsync: Kustomization,
) -> Kustomization:
    name = "public-coder-agent-backup"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            wait=True,
            path="./cluster/k8s/agents/public-coder-agent/backup",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="Bucket",
                    name="public-coder-agent-backups",
                    namespace="public-coder-agent",
                ),
                KustomizationSpecHealthChecks(
                    api_version="seaweed.seaweedfs.com/v1",
                    kind="S3Credentials",
                    name="public-coder-agent-backups",
                    namespace="public-coder-agent",
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="public-coder-agent-state-v2-restic",
                    namespace="public-coder-agent",
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(
                seaweedfs_public_coder_agent_backups_bucket, external_secrets_config, volsync
            ),
        ),
        description=(
            "Restic/VolSync backup of Public Coder's worker-local OpenClaw state "
            "to its dedicated private SeaweedFS S3 bucket."
        ),
    )


def public_coder_agent_devbox(
    chart: Chart,
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
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="30m",
            path="./cluster/k8s/agents/public-coder-agent/devbox",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=flux_kustomization_depends_on_many(
                kubevirt,
                forgejo_images,
                external_creds,
                external_secrets_config,
                agent_shared_secrets,
                public_coder_agent_app_kustomization,
            ),
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="kubevirt.io/v1",
                    kind="VirtualMachine",
                    name="public-coder-devbox",
                    namespace="public-coder-agent",
                ),
                KustomizationSpecHealthChecks(
                    api_version="external-secrets.io/v1",
                    kind="ExternalSecret",
                    name="buildbuddy-api-key",
                    namespace="public-coder-agent",
                ),
            ],
        ),
        description=(
            "KubeVirt build/test devbox for public-coder-agent "
            "(Bazel/BuildBuddy/direnv), with an ephemeral containerDisk root "
            "apart from the sshd host key and SSH access through ../sshpiper."
        ),
    )


def agent_shared_rbac(chart: Chart, claude_rbac: Kustomization, kyverno_policies: Kustomization) -> Kustomization:
    name = "agent-shared-rbac"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/shared-rbac",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=flux_kustomization_depends_on_many(claude_rbac, kyverno_policies),
        ),
        description=(
            "Cluster-scoped agent RBAC (ClusterRoleBindings) + flux-system "
            "RoleBindings only. Namespace-scoped RoleBindings live in per-service "
            "agent-rbac/ directories."
        ),
    )


def agent_shared_secrets(chart: Chart, claude_rbac: Kustomization) -> Kustomization:
    name = "agent-shared-secrets"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/shared-secrets",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=[flux_kustomization_depends_on(claude_rbac)],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def tana_mcp(
    chart: Chart, external_secrets_config: Kustomization, valkey: Kustomization, monitoring_crds: Kustomization
) -> Kustomization:
    name = "tana-mcp"
    return flux_kustomization(
        chart,
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            path="./cluster/k8s/agents/tana-mcp",
            prune=True,
            wait=True,
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="tana-mcp", namespace="tana-mcp"
                ),
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="tana-mcp-facade", namespace="tana-mcp"
                ),
            ],
            depends_on=flux_kustomization_depends_on_many(
                external_secrets_config,
                valkey,
                # ServiceMonitor + PrometheusRule
                monitoring_crds,
            ),
        ),
    )
