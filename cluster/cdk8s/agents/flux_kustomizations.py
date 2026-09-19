"""Flux Kustomizations for the cluster/k8s/agents slice."""

from __future__ import annotations

from pathlib import Path

from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpec,
    KustomizationSpecDecryption,
    KustomizationSpecDecryptionProvider,
    KustomizationSpecDecryptionSecretRef,
    KustomizationSpecDeletionPolicy,
    KustomizationSpecDependsOn,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.flux import flux_kustomization
from cluster.cdk8s.generation import write_yaml


def claude_rbac() -> dict[str, object]:
    # TODO: migrate this live Flux object name to agent-rbac-base in a staged
    # change. Renaming it directly would delete the old Kustomization and may prune
    # its inventory before the replacement owns the same RBAC resources.
    name = "claude-rbac"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/agent-rbac-base",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[KustomizationSpecDependsOn(name="kyverno-policies", namespace="ducktape-flux")],
            health_checks=[KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name="claude-sandbox")],
        ),
        description=(
            "Lightweight base for agent RBAC. Claude sandbox namespace + shared "
            "ClusterRoles. Must not depend on service or database kustomizations."
        ),
    )


def agent_sandbox_controller() -> dict[str, object]:
    name = "agent-sandbox-controller"
    return flux_kustomization(
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


def agent_workspaces_app() -> dict[str, object]:
    name = "agent-workspaces-app"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="agent-sandbox-controller",  # CRDs + controller
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="litellm-keys-tf",  # mints + reflects the Codex workspace key
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="kyverno-policies",  # cleanup-controller ClusterRole the janitor needs
                    namespace="ducktape-flux",
                ),
            ],
        ),
        description=(
            "Disposable agent workspace template + warm pool in agent-workspaces. "
            "See agents/agent-sandbox/README.md for usage."
        ),
    )


def airlock() -> dict[str, object]:
    name = "airlock"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
            ],
        ),
    )


def alloy_otlp_bearer() -> dict[str, object]:
    name = "alloy-otlp-bearer"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/alloy-otlp-bearer",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(
                    name="external-secrets-config",  # ClusterSecretStore + CRDs
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="claude-rbac",  # claude-sandbox namespace
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(
                    name="haku-rbac",  # haku-sandbox namespace
                    namespace="ducktape-flux",
                ),
            ],
            timeout="2m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def authentik_jwt_rotation() -> dict[str, object]:
    name = "authentik-jwt-rotation"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            path="./cluster/k8s/agents/authentik-jwt-rotation",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
            ],
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


def claude_sandbox_secrets() -> dict[str, object]:
    name = "claude-sandbox-secrets"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
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
            depends_on=[
                KustomizationSpecDependsOn(name="claude-rbac", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="ollama", namespace="ducktape-flux"),
            ],
        ),
    )


def coinbase_read() -> dict[str, object]:
    name = "coinbase-read"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/coinbase-read",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            depends_on=[
                # Reflector mirrors the Secret into haku-sandbox; haku-namespace creates
                # haku-sandbox (the reflection target).
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-namespace", namespace="ducktape-flux"),
            ],
        ),
    )


def forgejo_token_rotation() -> dict[str, object]:
    name = "forgejo-token-rotation"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/forgejo-token-rotation",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="authentik-jwt-rotation",  # owns the agents-infra namespace
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="forgejo-claude", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-state", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-agentydragon-repos", namespace="ducktape-flux"),
            ],
            timeout="2m",
        ),
    )


def haku_egress_proxy_namespace() -> dict[str, object]:
    name = "haku-egress-proxy-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/agents/haku-egress-proxy-namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def haku_egress_proxy() -> dict[str, object]:
    name = "haku-egress-proxy"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/haku-egress-proxy",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="haku-egress-proxy-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-openclaw-spike-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="haku-state",  # proxy-held Forgejo and Haku Console credentials
                    namespace="ducktape-flux",
                ),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-trust", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
            ],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def haku_openclaw_spike_app() -> dict[str, object]:
    name = "haku-openclaw-spike-app"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/agents/haku-openclaw-spike/app",
            prune=True,
            # Safety net for a planned cdk8s conversion of this directory that may move which
            # Kustomization owns an object: see cluster/cdk8s/AGENTS.md's two-step deletionPolicy
            # landing. This directory holds real PVCs (pvc.yaml, pvc-v2.yaml).
            deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="haku-openclaw-spike-namespace"),
                KustomizationSpecDependsOn(name="haku-egress-proxy", namespace="ducktape-flux"),
                # forgejo-images-creds-eso.yaml extracts the source Secret from the
                # forgejo-images namespace, so it must exist first.
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="flux-image-automation-forgejo", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
            ],
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


def haku_openclaw_spike_backup() -> dict[str, object]:
    name = "haku-openclaw-spike-backup"
    return flux_kustomization(
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
            depends_on=[
                # Backup/S3 wiring must converge even when the OpenClaw Deployment is down.
                # The Bucket and S3Credentials remain app-owned, but their readiness is
                # retried by the ExternalSecret rather than coupling this Kustomization to
                # the app Deployment health check.
                KustomizationSpecDependsOn(name="haku-openclaw-spike-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="seaweedfs-cluster", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="volsync", namespace="ducktape-flux"),
            ],
        ),
        description=(
            "Restic/VolSync backup of the Haku OpenClaw spike state to its "
            "dedicated private SeaweedFS S3 bucket, plus the one-shot restore "
            "into the optiplex worker PVC that migrates the state off the control "
            "plane."
        ),
    )


def haku_openclaw_spike_namespace() -> dict[str, object]:
    name = "haku-openclaw-spike-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/agents/haku-openclaw-spike/namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def kubectl_passthrough_mcp() -> dict[str, object]:
    name = "kubectl-passthrough-mcp"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                # TF writes the kubectl-passthrough-mcp secret (with config.toml) into the namespace.
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
            ],
        ),
    )


def loki_read_proxy() -> dict[str, object]:
    name = "loki-read-proxy"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            retry_interval="1m",
            interval="10m",
            timeout="5m",
            path="./cluster/k8s/agents/loki-read-proxy",
            prune=True,
            wait=True,
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
            ],
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


def agent_machine_access_tf() -> dict[str, object]:
    name = "agent-machine-access-tf"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="tofu-controller", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="tofu-state-db", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="authentik", namespace="ducktape-flux"),
            ],
        ),
    )


def agents_mitmproxy_namespace() -> dict[str, object]:
    name = "agents-mitmproxy-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/agents/mitmproxy-namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def agents_mitmproxy() -> dict[str, object]:
    name = "agents-mitmproxy"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/mitmproxy",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
            ),
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="agents-mitmproxy-namespace", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-trust", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
            ],
        ),
    )


def plaid_mcp() -> dict[str, object]:
    name = "plaid-mcp"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cnpg", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="local-path-provisioner", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # ServiceMonitor
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def public_coder_agent_app() -> dict[str, object]:
    name = "public-coder-agent-app"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/public-coder-agent/app",
            prune=True,
            # Safety net for a planned cdk8s conversion of this directory that may move which
            # Kustomization owns an object: see cluster/cdk8s/AGENTS.md's two-step deletionPolicy
            # landing. This directory holds real PVCs (pvc-v2.yaml, pvc-diagnostics.yaml).
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
            depends_on=[
                KustomizationSpecDependsOn(name="public-coder-agent-namespace", namespace="ducktape-flux"),
                # The proxy layer owns the trust Bundle this pod mounts, so it must land first.
                KustomizationSpecDependsOn(name="public-coder-agent-proxy", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="external-creds", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="litellm-keys-tf", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1", kind="Deployment", name="public-coder-agent", namespace="public-coder-agent"
                )
            ],
        ),
        description=(
            "OpenClaw coder agent at public-coder-agent.allegedly.works -- "
            "Authentik-gated, egress-confined to its own proxy."
        ),
    )


def public_coder_agent_backup() -> dict[str, object]:
    name = "public-coder-agent-backup"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(
                    name="seaweedfs-public-coder-agent-backups-bucket", namespace="ducktape-flux"
                ),
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="volsync", namespace="ducktape-flux"),
            ],
        ),
        description=(
            "Restic/VolSync backup of Public Coder's worker-local OpenClaw state "
            "to its dedicated private SeaweedFS S3 bucket."
        ),
    )


def public_coder_agent_devbox() -> dict[str, object]:
    name = "public-coder-agent-devbox"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="public-coder-agent-namespace", namespace="ducktape-flux"),
                # Owns the public-coder-agent-proxy-ca-cert ConfigMap this VM mounts as a guest disk.
                KustomizationSpecDependsOn(name="public-coder-agent-proxy", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="kubevirt", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="kubevirt.io/v1",
                    kind="VirtualMachine",
                    name="public-coder-devbox",
                    namespace="public-coder-agent",
                )
            ],
        ),
        description=(
            "KubeVirt build/test devbox for public-coder-agent "
            "(Bazel/BuildBuddy/direnv), with an ephemeral containerDisk root "
            "apart from the sshd host key and SSH access through ../sshpiper."
        ),
    )


def public_coder_agent_namespace() -> dict[str, object]:
    name = "public-coder-agent-namespace"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            path="./cluster/k8s/agents/public-coder-agent/namespace",
            prune=False,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
        ),
    )


def public_coder_agent_proxy() -> dict[str, object]:
    name = "public-coder-agent-proxy"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="1h",
            retry_interval="1m",
            path="./cluster/k8s/agents/public-coder-agent/proxy",
            prune=True,
            wait=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="public-coder-agent-namespace"),
                KustomizationSpecDependsOn(name="cert-manager-environment", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="cert-manager-trust", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                # Generates the proxy-held Haku Console static-Agent bearer after the target namespace exists.
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                # The standing access-profile RBAC and execution ceiling (formerly a separate
                # public-coder-agent-k8s-reader Kustomization) now live in public-coder-agent-app,
                # which itself depends on this proxy -- depending on it here would cycle. RBAC
                # objects have no pod-startup ordering requirement; if the ceiling lands after
                # this proxy, credential mediation is briefly unconstrained-by-that-check rather
                # than blocked, and self-heals on the next reconcile.
                # Console depends on haku-workspaces, which depends on this proxy. Do not point this edge back
                # at Console: the proxy can become Ready before its clients, while a cycle would prevent a
                # fresh cluster from ever reaching the claim-creation boundary.
                # The proxy reads the reflected Matrix bot password from its namespace.
                KustomizationSpecDependsOn(name="matrix", namespace="ducktape-flux"),
                # aiquota creates the sole bearer, then reflects its constrained mirror to
                # this namespace for the proxy-only substitution rule.
                KustomizationSpecDependsOn(name="aiquota", namespace="ducktape-flux"),
                # Mints the public-coder LiteLLM key reflected into this namespace for runner-proxy-only use.
                KustomizationSpecDependsOn(name="litellm-keys-tf", namespace="ducktape-flux"),
            ],
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="public-coder-agent-proxy",
                    namespace="public-coder-agent",
                ),
                KustomizationSpecHealthChecks(
                    api_version="cert-manager.io/v1",
                    kind="Certificate",
                    name="public-coder-agent-proxy-root-ca",
                    namespace="public-coder-agent",
                ),
            ],
        ),
        description="OpenClaw Iron proxy plus interception CA.",
    )


def public_coder_agent_sshpiper() -> dict[str, object]:
    name = "public-coder-agent-sshpiper"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="10m",
            path="./cluster/k8s/agents/public-coder-agent/sshpiper",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
            # Deliberately NOT public-coder-agent-devbox. The piper is an ordinary Deployment that does
            # not need the VM to exist; only a connection through it does. That Kustomization gates on a
            # KubeVirt VirtualMachine health check that regularly spends its full 30m timeout, and gating
            # on it meant the bastion did not deploy at all while the VM was unhealthy. A piper that runs
            # and fails a connection is both a better failure mode and a diagnosable one.
            depends_on=[
                KustomizationSpecDependsOn(name="public-coder-agent-namespace", namespace="ducktape-flux"),
                # Pipes are unschedulable until the CRD exists, and the plugin's watch fails without it.
                KustomizationSpecDependsOn(name="sshpiper-crds", namespace="ducktape-flux"),
            ],
            wait=True,
            health_checks=[
                KustomizationSpecHealthChecks(
                    api_version="apps/v1",
                    kind="Deployment",
                    name="public-coder-agent-sshpiper",
                    namespace="public-coder-agent",
                )
            ],
        ),
        description=(
            "Terminating SSH bastion for the Agent's route to its devbox. The "
            "Agent authenticates with a key valid only here; this Pod holds the "
            "key that opens coder@public-coder-devbox."
        ),
    )


def agent_shared_rbac() -> dict[str, object]:
    name = "agent-shared-rbac"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/shared-rbac",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="2m",
            depends_on=[
                KustomizationSpecDependsOn(name="claude-rbac", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="kyverno-policies", namespace="ducktape-flux"),
            ],
        ),
        description=(
            "Cluster-scoped agent RBAC (ClusterRoleBindings) + flux-system "
            "RoleBindings only. Namespace-scoped RoleBindings live in per-service "
            "agent-rbac/ directories."
        ),
    )


def agent_shared_secrets() -> dict[str, object]:
    name = "agent-shared-secrets"
    return flux_kustomization(
        name,
        spec=KustomizationSpec(
            interval="10m",
            path="./cluster/k8s/agents/shared-secrets",
            prune=True,
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=name, namespace="ducktape-flux"
            ),
            timeout="5m",
            depends_on=[KustomizationSpecDependsOn(name="claude-rbac", namespace="ducktape-flux")],
            decryption=KustomizationSpecDecryption(
                provider=KustomizationSpecDecryptionProvider.SOPS,
                secret_ref=KustomizationSpecDecryptionSecretRef(name="sops-age-cluster-secrets"),
            ),
        ),
    )


def tana_mcp() -> dict[str, object]:
    name = "tana-mcp"
    return flux_kustomization(
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
            depends_on=[
                KustomizationSpecDependsOn(name="external-secrets-config", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="forgejo-images", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="gateway", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="valkey", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="agent-machine-access-tf", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(name="reflector", namespace="ducktape-flux"),
                KustomizationSpecDependsOn(
                    name="monitoring-crds",  # ServiceMonitor + PrometheusRule
                    namespace="ducktape-flux",
                ),
            ],
        ),
    )


def write_manifests(root: Path) -> None:
    path = root / "cluster/k8s/agents/agent-rbac-base/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, claude_rbac())
    path = root / "cluster/k8s/agents/agent-sandbox/controller/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agent_sandbox_controller())
    path = root / "cluster/k8s/agents/agent-sandbox/workspaces/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agent_workspaces_app())
    path = root / "cluster/k8s/agents/airlock/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, airlock())
    path = root / "cluster/k8s/agents/alloy-otlp-bearer/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, alloy_otlp_bearer())
    path = root / "cluster/k8s/agents/authentik-jwt-rotation/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, authentik_jwt_rotation())
    path = root / "cluster/k8s/agents/claude-sandbox-secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, claude_sandbox_secrets())
    path = root / "cluster/k8s/agents/coinbase-read/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, coinbase_read())
    path = root / "cluster/k8s/agents/forgejo-token-rotation/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, forgejo_token_rotation())
    path = root / "cluster/k8s/agents/haku-egress-proxy-namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_egress_proxy_namespace())
    path = root / "cluster/k8s/agents/haku-egress-proxy/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_egress_proxy())
    path = root / "cluster/k8s/agents/haku-openclaw-spike/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_openclaw_spike_app())
    path = root / "cluster/k8s/agents/haku-openclaw-spike/backup/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_openclaw_spike_backup())
    path = root / "cluster/k8s/agents/haku-openclaw-spike/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, haku_openclaw_spike_namespace())
    path = root / "cluster/k8s/agents/kubectl-passthrough-mcp/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, kubectl_passthrough_mcp())
    path = root / "cluster/k8s/agents/loki-read-proxy/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, loki_read_proxy())
    path = root / "cluster/k8s/agents/machine-access-tf/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agent_machine_access_tf())
    path = root / "cluster/k8s/agents/mitmproxy-namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agents_mitmproxy_namespace())
    path = root / "cluster/k8s/agents/mitmproxy/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agents_mitmproxy())
    path = root / "cluster/k8s/agents/plaid-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, plaid_mcp())
    path = root / "cluster/k8s/agents/public-coder-agent/app/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, public_coder_agent_app())
    path = root / "cluster/k8s/agents/public-coder-agent/backup/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, public_coder_agent_backup())
    path = root / "cluster/k8s/agents/public-coder-agent/devbox/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, public_coder_agent_devbox())
    path = root / "cluster/k8s/agents/public-coder-agent/namespace/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, public_coder_agent_namespace())
    path = root / "cluster/k8s/agents/public-coder-agent/proxy/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, public_coder_agent_proxy())
    path = root / "cluster/k8s/agents/public-coder-agent/sshpiper/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, public_coder_agent_sshpiper())
    path = root / "cluster/k8s/agents/shared-rbac/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agent_shared_rbac())
    path = root / "cluster/k8s/agents/shared-secrets/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, agent_shared_secrets())
    path = root / "cluster/k8s/agents/tana-mcp/flux-kustomization.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_yaml(path, tana_mcp())
