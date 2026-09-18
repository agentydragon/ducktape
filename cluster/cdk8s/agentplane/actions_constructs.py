"""Reusable cdk8s constructs for the Agentplane staging/testing environments' actions/
directory: the Action Service Deployment (+ Alembic migrate initContainer), its own
RBAC, ConfigMaps, Service, HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget.

Environment-specific pieces (staging's ActionPolicySet/Binding objects and claude-ai
ServiceAccount; testing's mcp-everything/oauth-fixture) live in sibling modules and are
added to the same Chart alongside this construct -- see generate_manifests.py.

The Deployment's image tags are deliberate placeholders ("unset") -- the sibling
image-pins/ Kustomize Component (hand-written, never generated) overrides them at
`kustomize build` time via Flux's image-automation marker. See cluster/docs/cdk8s.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    PodSecurityContextProps,
    Protocol,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    SecretValue,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
    k8s,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct

from cluster.cdk8s.agentplane import (
    cilium_helpers,
    container_security,
    db_constructs,
    llm_ingress_constructs,
    node_scheduling,
)
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.config_format import json5_config, yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from x.agentplane.action_service.main import CONFIG_FILE_ENV, Settings
from x.agentplane.app import main as app_main
from x.agentplane.settings_contract import checked_value, cli_args, env_name, settings_file

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-actions"
_ACTIONS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-action-service"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-action-service-migrate"
CONTAINER_PORT = 8080
_LABELS = {"app.kubernetes.io/name": _NAME}
_SETTINGS_DIR = "/etc/agentplane-actions"
_MCP_PATHS = (
    "/mcp",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource/mcp",
    "/register",
    "/authorize",
    "/token",
    "/revoke",
    "/auth/callback",
)


# cdk8s_plus_34's Python stub doesn't declare ApiResource as implementing
# IApiResource's `resource_name` member (see namespace_rbac_constructs.py's `_custom`,
# same cast for the same reason).
def _custom(api_group: str, resource_type: str) -> IApiResource:
    return cast(IApiResource, ApiResource.custom(api_group=api_group, resource_type=resource_type))


@dataclass(frozen=True)
class ActionsEnvSpec:
    """Per-environment values for the Action Service."""

    namespace: str
    replicas: int
    strategy: DeploymentStrategy
    min_ready: Duration | None
    # staging spreads its 2 replicas across nodes; testing's single replica has
    # nothing to spread.
    topology_spread: bool
    pdb_min_available: int | None
    hostname: str
    settings: dict
    action_federation: dict
    action_federation_description: str
    operator_oidc: dict
    # Secrets whose rotation should roll the Deployment, beyond agentplane-mcp-oauth
    # (always reloaded) -- staging also reloads its web-push and GitHub MCP client
    # credentials and the ssh-mcp bearer.
    extra_reload_secrets: Sequence[str] = ()
    # Keys mounted from the agentplane-mcp-oauth Secret at /etc/agentplane-mcp: staging
    # needs the full OAuth linkage triad, testing only the one MCP client's secret.
    oauth_secret_items: Sequence[str] = ("client-secret",)
    # Staging-only extras; None/False omits the corresponding env var, volume, and mount.
    web_push_secret_name: str | None = None
    github_mcp_client_secret_name: str | None = None
    ssh_mcp_bearer: bool = False
    # Additional environment-specific CiliumNetworkPolicy egress rules (the remote-node/
    # host :443 rule reaching this environment's OIDC provider, push services,
    # GitHub MCP hosts, kubectl-passthrough-mcp, the in-cluster Authentik Service, the
    # testing oauth-fixture Service, ...), appended after the shared DNS/claude.ai/
    # kube-apiserver/postgres rules.
    extra_egress: Sequence[CiliumNetworkPolicySpecEgress] = field(default_factory=tuple)


class Actions(Construct):
    """ServiceAccount, RBAC, ConfigMaps, Deployment (+ migrate initContainer), Service,
    HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget for the Action Service.
    """

    def __init__(self, scope: Construct, id: str, spec: ActionsEnvSpec) -> None:
        super().__init__(scope, id)
        self.spec = spec

        service_account = self._add_service_account()
        self._add_rbac(service_account)
        settings_cm, action_federation_cm = self._add_configmaps()
        deployment = self._add_deployment(service_account, settings_cm, action_federation_cm)
        self._add_service(deployment)
        self._add_http_route()
        self._add_network_policy()
        if spec.pdb_min_available is not None:
            self._add_pdb(spec.pdb_min_available)

    def _add_service_account(self) -> ServiceAccount:
        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the Action
        # Service calls TokenReview as itself, so it needs its own mounted token.
        return ServiceAccount(
            self, "serviceaccount", metadata=metadata(_NAME, self.spec.namespace), automount_token=True
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.spec.namespace
        # TokenReview proves the Pod-bound workload bearer the central egress proxy
        # forwards, and the session-bound bearer the app forwards for its BFF/operator
        # adapter. Creating a review grants none of the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-actions-token-reviewer",
            service_account_name=_NAME,
            namespace=namespace,
        )
        # The policy informer watches the sets and bindings it evaluates and the
        # labeled caller ServiceAccounts, and writes back only each object's Ready
        # condition. No Pods, no Thread/Agent reads, no Secrets.
        Role(
            self,
            "role",
            metadata=metadata(_NAME, namespace),
            rules=[
                RolePolicyRule(resources=[_custom("", "serviceaccounts")], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[
                        _custom("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets", "actionpolicybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[
                        _custom("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets/status", "actionpolicybindings/status")
                    ],
                    verbs=["patch"],
                ),
            ],
        )
        RoleBinding(
            self, "rolebinding", metadata=metadata(_NAME, namespace), role=Role.from_role_name(self, "role-ref", _NAME)
        ).add_subjects(service_account)

    def _add_configmaps(self) -> tuple[ConfigMap, ConfigMap]:
        settings_cm = ConfigMap(
            self,
            "settings",
            metadata=metadata("agentplane-actions-settings", self.spec.namespace),
            data={"settings.yaml": yaml_config(settings_file(Settings, self.spec.settings))},
        )
        action_federation_cm = ConfigMap(
            self,
            "action-federation",
            metadata=metadata(
                "agentplane-action-federation",
                self.spec.namespace,
                annotations={"description": self.spec.action_federation_description},
            ),
            data={
                # Each key is one reader's field: the app's `action_federation`, this service's `operator_oidc`.
                "action-federation": json5_config(
                    checked_value(app_main.Settings, "action_federation", self.spec.action_federation)
                ),
                "operator-oidc": json5_config(checked_value(Settings, "operator_oidc", self.spec.operator_oidc)),
            },
        )
        return settings_cm, action_federation_cm

    def _database_env(self) -> dict[str, EnvValue]:
        postgres_actions = Secret.from_secret_name(self, "postgres-actions-secret", "postgres-actions")
        return {
            "AGENTPLANE_ACTIONS_DB_USER": EnvValue.from_secret_value(
                SecretValue(secret=postgres_actions, key="username")
            ),
            "AGENTPLANE_ACTIONS_DB_PASSWORD": EnvValue.from_secret_value(
                SecretValue(secret=postgres_actions, key="password")
            ),
            "AGENTPLANE_ACTIONS_DB_HOST": EnvValue.from_secret_value(SecretValue(secret=postgres_actions, key="host")),
            "AGENTPLANE_ACTIONS_DB_PORT": EnvValue.from_secret_value(SecretValue(secret=postgres_actions, key="port")),
            "AGENTPLANE_ACTIONS_DB_NAME": EnvValue.from_secret_value(
                SecretValue(secret=postgres_actions, key="dbname")
            ),
            env_name(Settings, "database_url"): EnvValue.from_value(
                "postgresql://$(AGENTPLANE_ACTIONS_DB_USER):$(AGENTPLANE_ACTIONS_DB_PASSWORD)"
                "@$(AGENTPLANE_ACTIONS_DB_HOST):$(AGENTPLANE_ACTIONS_DB_PORT)/$(AGENTPLANE_ACTIONS_DB_NAME)"
            ),
        }

    def _container_env(self, action_federation_cm: ConfigMap) -> dict[str, EnvValue]:
        env = self._database_env()
        env[env_name(Settings, "operator_oidc")] = EnvValue.from_config_map(action_federation_cm, "operator-oidc")
        env[CONFIG_FILE_ENV] = EnvValue.from_value(f"{_SETTINGS_DIR}/settings.yaml")
        if self.spec.web_push_secret_name is not None:
            web_push_secret = Secret.from_secret_name(self, "web-push-secret", self.spec.web_push_secret_name)
            env[env_name(Settings, "web_push", "private_key_pem")] = EnvValue.from_secret_value(
                SecretValue(secret=web_push_secret, key="private-key-pem")
            )
        if self.spec.github_mcp_client_secret_name is not None:
            oauth_secret = Secret.from_secret_name(self, "mcp-oauth-secret-env", "agentplane-mcp-oauth")
            env[env_name(Settings, "oauth")] = EnvValue.from_secret_value(SecretValue(secret=oauth_secret, key="oauth"))
            github_secret = Secret.from_secret_name(
                self, "github-mcp-client-secret-env", self.spec.github_mcp_client_secret_name
            )
            env[env_name(Settings, "mcp_servers", "github", "client_id")] = EnvValue.from_secret_value(
                SecretValue(secret=github_secret, key="client_id")
            )
        return env

    def _add_deployment(
        self, service_account: ServiceAccount, settings_cm: ConfigMap, action_federation_cm: ConfigMap
    ) -> Deployment:
        namespace = self.spec.namespace
        env = self._container_env(action_federation_cm)
        secret_reload = ",".join(["agentplane-mcp-oauth", *self.spec.extra_reload_secrets])

        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME,
                namespace,
                labels=_LABELS,
                annotations={
                    "secret.reloader.stakater.com/reload": secret_reload,
                    # agentplane-actions-settings has no kustomize configMapGenerator hash
                    # here to roll the Deployment on content changes (see
                    # cluster/docs/cdk8s.md); reloader covers that gap explicitly.
                    "configmap.reloader.stakater.com/reload": "agentplane-action-federation,agentplane-actions-settings",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.spec.replicas,
            strategy=self.spec.strategy,
            min_ready=self.spec.min_ready,
            # 20s execution drain + 5s forced persistence, with room for HTTP/adapter teardown.
            termination_grace_period=Duration.seconds(60),
            service_account=service_account,
            # cdk8s_plus_34 defaults this to False independent of the ServiceAccount's
            # own automount_token -- opt in for the same reason as the ServiceAccount.
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            init_containers=[migrate_init_container(f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}", env_variables=env)],
        )
        deployment.add_container(
            name="actions",
            image=f"{_ACTIONS_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            args=cli_args(
                Settings,
                host="0.0.0.0",
                port=CONTAINER_PORT,
                token_audience=llm_ingress_constructs.WORKLOAD_TOKEN_AUDIENCE,
            ),
            env_variables=env,
            ports=[ContainerPort(name="http", number=CONTAINER_PORT, protocol=Protocol.TCP)],
            readiness=http_probe(
                "/readyz", port=CONTAINER_PORT, initial_delay_seconds=3, period_seconds=10, timeout_seconds=5
            ),
            liveness=http_probe(
                "/healthz", port=CONTAINER_PORT, initial_delay_seconds=20, period_seconds=30, timeout_seconds=5
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )

        oauth_secret = Secret.from_secret_name(self, "mcp-oauth-secret", "agentplane-mcp-oauth")
        oauth_volume = Volume.from_secret(
            self,
            "oauth-volume",
            oauth_secret,
            default_mode=0o440,
            items={key: PathMapping(path=key) for key in self.spec.oauth_secret_items},
        )
        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount("/etc/agentplane-mcp", oauth_volume, read_only=True)
        deployment.containers[0].mount(_SETTINGS_DIR, settings_volume, read_only=True)
        if self.spec.github_mcp_client_secret_name is not None:
            github_secret = Secret.from_secret_name(
                self, "github-mcp-client-secret", self.spec.github_mcp_client_secret_name
            )
            github_volume = Volume.from_secret(
                self,
                "github-mcp-client-volume",
                github_secret,
                default_mode=0o440,
                items={"client_secret": PathMapping(path="client_secret")},
            )
            deployment.containers[0].mount("/etc/agentplane-github", github_volume, read_only=True)
        if self.spec.ssh_mcp_bearer:
            ssh_mcp_secret = Secret.from_secret_name(self, "ssh-mcp-bearer-secret", "ssh-mcp-bearer")
            ssh_mcp_volume = Volume.from_secret(
                self, "ssh-mcp-bearer-volume", ssh_mcp_secret, items={"bearer-token": PathMapping(path="bearer-token")}
            )
            # Its own directory: a subPath file cannot be mounted inside the read-only
            # settings volume (runc: "not a directory").
            deployment.containers[0].mount("/run/secrets/ssh-mcp", ssh_mcp_volume, read_only=True)

        node_scheduling.attract_to_zone(deployment)
        apply_pod_spec_patches(deployment, labels=_LABELS, topology_spread=self.spec.topology_spread)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.spec.namespace),
            selector=deployment,
            ports=[ServicePort(name="http", port=CONTAINER_PORT, target_port=CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_http_route(self) -> None:
        # The Actions service owns OAuth and bearer verification; no browser forward-auth
        # hop. Keep REST/operator endpoints off this public origin.
        https_route(
            self,
            "httproute",
            metadata=metadata(f"{_NAME}-mcp", self.spec.namespace),
            hostname=self.spec.hostname,
            backend=_NAME,
            port=CONTAINER_PORT,
            paths=_MCP_PATHS,
            timeout="3600s",
        )

    def _add_network_policy(self) -> None:
        namespace = self.spec.namespace
        cilium_helpers.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(_NAME, namespace),
            selector=_LABELS,
            ingress=[
                cilium_helpers.ingress_from_gateway(CONTAINER_PORT),
                cilium_helpers.ingress_from(
                    cilium_helpers.endpoint_labels(namespace, "agentplane-egress"), ports=[CONTAINER_PORT]
                ),
                cilium_helpers.ingress_from(
                    cilium_helpers.endpoint_labels(namespace, "agentplane-app"), ports=[CONTAINER_PORT]
                ),
            ],
            egress=[
                cilium_helpers.dns_egress(l7=True),
                # Claude's credentialless CIMD document; no wildcard hosts, ports, or redirects.
                cilium_helpers.egress_to_fqdns("claude.ai"),
                cilium_helpers.egress_to_entities("kube-apiserver"),
                cilium_helpers.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    db_constructs.POSTGRES_PORT,
                ),
                *self.spec.extra_egress,
            ],
        )

    def _add_pdb(self, min_available: int) -> None:
        k8s.KubePodDisruptionBudget(
            self,
            "pdb",
            metadata=k8s.ObjectMeta(name=_NAME, namespace=self.spec.namespace),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(min_available),
                selector=k8s.LabelSelector(match_labels=_LABELS),
            ),
        )
