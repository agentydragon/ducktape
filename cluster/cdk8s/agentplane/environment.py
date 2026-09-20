"""One `Environment` per deployed Agentplane namespace (staging.py, testing.py). Every
construct in this package takes the whole environment and reads what it needs, so a value
two constructs share (the egress CA name the runner template mounts, the namespace) is
written once.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from cdk8s import Duration
from cdk8s_plus_34 import DeploymentStrategy
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress

# What every environment's Flux Kustomization waits on.
DEPENDS_ON = (
    "agentplane-crds",
    "agent-sandbox-controller",
    "cert-manager-environment",
    "cert-manager-trust",
    "claude-rbac",
    "cnpg",
    "external-creds",
    "external-secrets-config",
    "forgejo-images",
    "gateway",
    "litellm-keys-tf",
    "local-path-provisioner",
    "reflector",
)


@dataclass(frozen=True)
class ReplicaProfile:
    """The replicas/strategy/minReadySeconds/PDB shape shared by every Deployment in an
    environment (llm-ingress, egress, app, actions)."""

    count: int
    strategy: DeploymentStrategy
    min_ready: Duration | None
    # PodDisruptionBudget minAvailable, or None for no budget (one replica has nothing to
    # keep). Sized so a zero-maxUnavailable rollout never trips it.
    pdb_min_available: int | None


@dataclass(frozen=True)
class DbProps:
    instances: int
    # Preferred pod anti-affinity across nodes; a single instance sets none of CNPG's
    # three affinity fields at all (not just false).
    pod_anti_affinity: bool


@dataclass(frozen=True)
class LlmIngressProps:
    litellm_key_secret_name: str


@dataclass(frozen=True)
class EgressProps:
    # The interception CA's Secret/Bundle/ConfigMap name; the runner SandboxTemplate
    # mounts the ConfigMap by the same name.
    ca_secret_name: str


@dataclass(frozen=True)
class AppProps:
    hostname: str
    oidc_issuer: str
    # staging's OIDC provider is the in-cluster Authentik Service, reached both by its
    # public hostname and directly; testing's Dex is reached only by its public hostname.
    reach_incluster_authentik: bool
    # Pin runner Pods to a zone (near the database/LiteLLM), or None for no pin.
    runner_zone: str | None


@dataclass(frozen=True)
class BearerMcpMount:
    """One static-bearer MCP backend's reflected Secret, mounted at
    `/run/secrets/<name>/<file_name>` for an `action_groups` entry's `bearer_file` to name."""

    name: str
    secret_name: str
    secret_key: str
    file_name: str = "bearer-token"


@dataclass(frozen=True)
class ActionsProps:
    hostname: str
    # agentplane/action_service `Settings`, the settings ConfigMap; `operator_oidc` included.
    settings: dict
    # Secrets whose rotation should roll the Deployment, beyond agentplane-mcp-oauth
    # (always reloaded).
    extra_reload_secrets: Sequence[str] = ()
    # Keys mounted from the agentplane-mcp-oauth Secret at /etc/agentplane-mcp.
    oauth_secret_items: Sequence[str] = ("client-secret",)
    # None/False omits the corresponding env var, volume, and mount.
    web_push_secret_name: str | None = None
    github_mcp_client_secret_name: str | None = None
    bearer_mcp_mounts: Sequence[BearerMcpMount] = ()
    # CiliumNetworkPolicy egress rules appended after the shared DNS/claude.ai/
    # kube-apiserver/postgres rules: this environment's OIDC provider, MCP servers, ...
    extra_egress: Sequence[CiliumNetworkPolicySpecEgress] = field(default_factory=tuple)


@dataclass(frozen=True)
class Environment:
    namespace: str
    # The Namespace's `description` annotation and the Flux Kustomization's.
    description: str
    flux_description: str
    depends_on: Sequence[str]
    # Hand-written files the root Kustomization lists beside the generated one.
    extra_resources: Sequence[str]
    # Whether the operator Role may manage ActionPolicySet/Binding objects.
    include_action_policy_rule: bool
    replicas: ReplicaProfile
    # agentplane/app/main.py's `Settings`, the `agentplane-app-config` ConfigMap.
    app_config: dict
    db: DbProps
    llm_ingress: LlmIngressProps
    egress: EgressProps
    app: AppProps
    actions: ActionsProps
