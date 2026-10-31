"""One `Environment` per Agentplane stack (staging.py, the k3s fixture). Every
construct in this package takes the whole environment and reads what it needs, so a value
two constructs share (the egress CA name the runner template mounts, the namespace) is
written once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cdk8s import Duration
from cdk8s_plus_34 import DeploymentStrategy
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress

from agentplane.action_service.catalog import ActionGroup, Key
from agentplane.action_service.mcp_settings import McpClientMetadataSettings, McpOAuthServer
from agentplane.action_service.operator_oidc_settings import OperatorOidcSettings
from agentplane.app.settings import AppSettingsConfig
from agentplane.llm_ingress.models import AttributionConfig, ModelConfig, default_attribution
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.runner.configuration import GuestResources
from agentplane.subjects import ServiceAccountRef
from agentplane.x.central_sampler import settings
from cluster.cdk8s.secret_ref import SecretKey
from model_catalog.catalog import Route


@dataclass(frozen=True)
class ReplicaProfile:
    """A Deployment's replica count, rollout strategy, readiness delay, and eviction budget."""

    count: int
    strategy: DeploymentStrategy
    min_ready: Duration | None
    # PodDisruptionBudget minAvailable for voluntary evictions, or None for no budget.
    pdb_min_available: int | None


@dataclass(frozen=True)
class LlmIngressProps:
    litellm_key_secret_name: str
    models: Sequence[ModelConfig]
    # The routes the Codex harness offers; the ingress serves Codex a catalog entry for those that need one.
    codex_routes: Sequence[Route]
    attribution: AttributionConfig = field(default_factory=default_attribution)
    log_llm_requests: bool = False


@dataclass(frozen=True)
class CentralSamplerProps:
    """The opt-in central code-mode service's model contracts and runtime limits."""

    models: Mapping[str, settings.ModelConfig]
    connections: Mapping[str, settings.ConnectionConfig]
    max_live_sessions: int
    turn_timeout_seconds: int
    rpc_timeout_seconds: int
    lease_seconds: int
    # The settings carry each credential file path; this map names only the Secret key
    # projected there, keyed by connection name.
    connection_credentials: Mapping[str, SecretKey] = field(default_factory=dict)
    executor_initialize_timeout_seconds: int = settings.DEFAULT_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS
    auto_compact_tokens: int | None = None


@dataclass(frozen=True)
class EgressProps:
    # The interception CA's Secret/Bundle/ConfigMap name; the runner SandboxTemplate
    # mounts the ConfigMap by the same name.
    ca_secret_name: str
    credentials_namespace: str
    replicas: ReplicaProfile
    traffic_distribution: str | None
    # Restrict placement and spread across these zones; None retains caller placement.
    zones: tuple[str, ...] | None = None
    # Static relay clients outside the runner namespace; admission is not an egress grant.
    external_workload_namespaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppProps:
    # Where a browser reaches the app, which OIDC redirects return to.
    origin: str
    oidc_issuer: str
    # staging's OIDC provider is the in-cluster Authentik Service, reached both by its
    # public hostname and directly.
    reach_incluster_authentik: bool
    # Pin runner Pods to a zone (near the database/LiteLLM), or None for no pin.
    runner_zone: str | None
    # The OIDC client secret and the session signing key can have separate owners.
    # The k3s fixture leaves this at `agentplane-oidc`; staging uses an ESO-generated Secret.
    oidc_session_secret_name: str = "agentplane-oidc"

    @property
    def hostname(self) -> str:
        hostname = urlsplit(self.origin).hostname
        assert hostname is not None, f"app origin has no hostname: {self.origin!r}"
        return hostname


@dataclass(frozen=True)
class BearerMcpMount:
    """The environment's copy of one static-bearer MCP backend's bearer Secret, mounted at
    `/run/secrets/<name>/<file_name>` for an `action_groups` entry's `bearer_file` to name."""

    name: str
    secret_name: str
    secret_key: str
    file_name: str = "bearer-token"
    optional: bool = False


@dataclass(frozen=True)
class WebPushProps:
    """The Action Service's Web Push identity; its VAPID private key is `secret_name`'s
    `private-key-pem`."""

    subject: str
    public_base_url: str
    allowed_push_hosts: Sequence[str]
    secret_name: str


@dataclass(frozen=True)
class OrchestrationProps:
    """This environment offers the `orchestration_v1` ActionGroup, a stopgap
    (agentplane/action_service/orchestration_v1/README.md). Its launches take the app's presets and
    default PolicySets."""

    # The group's Actions external Connections also see as MCP tools of their own; None offers none.
    direct_tools: frozenset[Key] | None = None


@dataclass(frozen=True)
class ActionsProps:
    """This environment's Actions settings and secret wiring."""

    operator_oidc: OperatorOidcSettings
    # Its own namespace included; the Action Service watches policies in its own.
    caller_service_account_namespaces: frozenset[str]
    mcp_servers: Mapping[Key, McpOAuthServer]
    action_groups: Mapping[Key, ActionGroup]
    # Public origin for upstream client metadata; None omits public exposure.
    hostname: str | None = None
    mcp_client_metadata: McpClientMetadataSettings | None = None
    web_push: WebPushProps | None = None
    # Keys for upstream MCP OAuth clients mounted for Action Service linkage; none mounts nothing.
    oauth_secret_items: Sequence[str] = ()
    # None omits the corresponding env var, volume, and mount.
    github_mcp_client_secret_name: str | None = None
    bearer_mcp_mounts: Sequence[BearerMcpMount] = ()
    # CiliumNetworkPolicy egress rules appended after the shared DNS/kube-apiserver/
    # postgres rules: this environment's OIDC provider, MCP servers, ...
    extra_egress: Sequence[CiliumNetworkPolicySpecEgress] = field(default_factory=tuple)
    # None offers no orchestration_v1 group.
    orchestration_v1: OrchestrationProps | None = None


@dataclass(frozen=True)
class McpEdgeProps:
    """The independently staged MCP/Connection authority; no Actions settings imply its scope."""

    operator_oidc: OperatorOidcSettings
    caller_service_account_namespaces: frozenset[str]
    mintable_accounts: frozenset[ServiceAccountRef]
    oauth_secret_name: str | None
    hostname: str | None
    replicas: ReplicaProfile | None = None
    extra_egress: Sequence[CiliumNetworkPolicySpecEgress] = field(default_factory=tuple)


@dataclass(frozen=True)
class GitHubAppProps:
    app_id: int
    secret_name: str


@dataclass(frozen=True)
class Environment:
    namespace: str
    # The Namespace's `description` annotation.
    description: str
    replicas: ReplicaProfile
    # The ConfigMap-authored portion of agentplane/app/settings.py's `Settings`.
    app_config: AppSettingsConfig
    # Whether the History Service's writers remove an item's streamed delta entries once its
    # completion provably holds them (agentplane/history_service/README.md#delta-settlement).
    settle_deltas: bool
    llm_ingress: LlmIngressProps
    egress: EgressProps
    app: AppProps
    actions: ActionsProps
    mcp_edge: McpEdgeProps
    notifications_debounce: NoticeDebounceSettings
    # Whether this environment has VM Sandboxes: the runner VM template (runner_vm.py) and the egress
    # relay for their KubeVirt launchers (launcher_relay.py).
    vm_sandboxes: bool
    # None keeps the central code-mode service, its route, and its cross-service grants absent.
    central_sampler: CentralSamplerProps | None = None
    # Optional StorageClass for both claims owned by a VM Sandbox. If unset, they use the runner
    # template's state claim class.
    vm_storage_class: str | None = None
    # Guest sizing and Runner cgroup budgets shared by the VM template and guest config seed.
    vm_resources: GuestResources | None = None
    # Whether runner Pods delegate Codex command execution to a companion with its own PID namespace.
    isolated_codex_executor: bool = True
    # Whether a VM Sandbox's serial console is a root shell, for whoever holds the VMI `console`
    # subresource. Only meaningful with `vm_sandboxes`.
    vm_console_autologin: bool = False
    # Authenticated service callers permitted to wake only Sandboxes whose own `wakeOnSend` is true.
    wake_caller_accounts: frozenset[ServiceAccountRef] = frozenset()
    notifications_github: GitHubAppProps | None = None
    # Binding scopes delegated (binding_delegation.py) after their catalog entries are removed, so
    # the bindings existing Sandboxes still hold can be cleaned up.
    kubernetes_binding_cleanup_namespaces: frozenset[str] = frozenset()
    # Non-secret environment defaults shared by every SandboxTemplate workload container;
    # the common pod builder applies them, and the runner explicitly forwards these names to harnesses.
    sandbox_workload_env: Mapping[str, str] = field(default_factory=lambda: {"TZ": "America/Los_Angeles"})
    # Image-owned values explicitly inherited by harnesses. Runtime configuration can override
    # these through harness_environment.values; undeclared Runner variables reach no harness.
    # Nixpkgs glibc needs TZDIR to resolve the deployment's TZ against the image's zoneinfo.
    harness_inherited_env: Sequence[str] = ("TZDIR",)

    def __post_init__(self) -> None:
        if self.vm_storage_class is not None:
            if not self.vm_storage_class:
                raise ValueError("vm_storage_class cannot be empty")
            if not self.vm_sandboxes:
                raise ValueError("vm_storage_class requires vm_sandboxes")
