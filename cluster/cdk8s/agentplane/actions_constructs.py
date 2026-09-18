"""The Action Service: its Deployment (+ Alembic migrate initContainer), RBAC, ConfigMaps,
Service, HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget. Environment-only
objects (staging's policy sets, testing's MCP fixtures) come from `Environment.extra`.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ConfigMap,
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
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
from constructs import Construct

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import container_security, db_constructs, llm_ingress_constructs, node_scheduling
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.forgejo_images import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name, settings_file
from x.agentplane.action_service.main import CONFIG_FILE_ENV, Settings

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


class Actions(Construct):
    """ServiceAccount, RBAC, ConfigMaps, Deployment (+ migrate initContainer), Service,
    HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget for the Action Service.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env

        service_account = self._add_service_account()
        self._add_rbac(service_account)
        settings_cm = self._add_settings()
        deployment = self._add_deployment(service_account, settings_cm)
        self._add_service(deployment)
        self._add_http_route()
        self._add_network_policy()
        if env.replicas.pdb_min_available is not None:
            self._add_pdb(env.replicas.pdb_min_available)

    def _add_service_account(self) -> ServiceAccount:
        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the Action
        # Service calls TokenReview as itself, so it needs its own mounted token.
        return ServiceAccount(
            self, "serviceaccount", metadata=metadata(_NAME, self.env.namespace), automount_token=True
        )

    def _add_rbac(self, service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
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
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets", "actionpolicybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets/status", "actionpolicybindings/status")
                    ],
                    verbs=["patch"],
                ),
            ],
        )
        RoleBinding(
            self, "rolebinding", metadata=metadata(_NAME, namespace), role=Role.from_role_name(self, "role-ref", _NAME)
        ).add_subjects(service_account)

    def _add_settings(self) -> ConfigMap:
        return ConfigMap(
            self,
            "settings",
            metadata=metadata("agentplane-actions-settings", self.env.namespace),
            data={"settings.yaml": yaml_config(settings_file(Settings, self.env.actions.settings))},
        )

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

    def _container_env(self) -> dict[str, EnvValue]:
        env = self._database_env()
        env[CONFIG_FILE_ENV] = EnvValue.from_value(f"{_SETTINGS_DIR}/settings.yaml")
        if self.env.actions.web_push_secret_name is not None:
            web_push_secret = Secret.from_secret_name(self, "web-push-secret", self.env.actions.web_push_secret_name)
            env[env_name(Settings, "web_push", "private_key_pem")] = EnvValue.from_secret_value(
                SecretValue(secret=web_push_secret, key="private-key-pem")
            )
        if self.env.actions.github_mcp_client_secret_name is not None:
            oauth_secret = Secret.from_secret_name(self, "mcp-oauth-secret-env", "agentplane-mcp-oauth")
            env[env_name(Settings, "oauth")] = EnvValue.from_secret_value(SecretValue(secret=oauth_secret, key="oauth"))
            github_secret = Secret.from_secret_name(
                self, "github-mcp-client-secret-env", self.env.actions.github_mcp_client_secret_name
            )
            env[env_name(Settings, "mcp_servers", "github", "client_id")] = EnvValue.from_secret_value(
                SecretValue(secret=github_secret, key="client_id")
            )
        return env

    def _add_deployment(self, service_account: ServiceAccount, settings_cm: ConfigMap) -> Deployment:
        namespace = self.env.namespace
        env = self._container_env()
        secret_reload = ",".join(["agentplane-mcp-oauth", *self.env.actions.extra_reload_secrets])

        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME,
                namespace,
                labels=_LABELS,
                annotations={
                    "secret.reloader.stakater.com/reload": secret_reload,
                    # No configMapGenerator hash rolls the Deployment on settings changes
                    # (cluster/docs/cdk8s.md); reloader does.
                    "configmap.reloader.stakater.com/reload": "agentplane-actions-settings",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.env.replicas.count,
            strategy=self.env.replicas.strategy,
            min_ready=self.env.replicas.min_ready,
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
            items={key: PathMapping(path=key) for key in self.env.actions.oauth_secret_items},
        )
        settings_volume = Volume.from_config_map(self, "settings-volume", settings_cm)
        deployment.containers[0].mount("/etc/agentplane-mcp", oauth_volume, read_only=True)
        deployment.containers[0].mount(_SETTINGS_DIR, settings_volume, read_only=True)
        if self.env.actions.github_mcp_client_secret_name is not None:
            github_secret = Secret.from_secret_name(
                self, "github-mcp-client-secret", self.env.actions.github_mcp_client_secret_name
            )
            github_volume = Volume.from_secret(
                self,
                "github-mcp-client-volume",
                github_secret,
                default_mode=0o440,
                items={"client_secret": PathMapping(path="client_secret")},
            )
            deployment.containers[0].mount("/etc/agentplane-github", github_volume, read_only=True)
        if self.env.actions.ssh_mcp_bearer:
            ssh_mcp_secret = Secret.from_secret_name(self, "ssh-mcp-bearer-secret", "ssh-mcp-bearer")
            ssh_mcp_volume = Volume.from_secret(
                self, "ssh-mcp-bearer-volume", ssh_mcp_secret, items={"bearer-token": PathMapping(path="bearer-token")}
            )
            # Its own directory: a subPath file cannot be mounted inside the read-only
            # settings volume (runc: "not a directory").
            deployment.containers[0].mount("/run/secrets/ssh-mcp", ssh_mcp_volume, read_only=True)

        node_scheduling.attract_to_zone(deployment)
        apply_pod_spec_patches(deployment, labels=_LABELS, topology_spread=self.env.replicas.topology_spread)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.env.namespace),
            selector=deployment,
            ports=[ServicePort(name="http", port=CONTAINER_PORT, target_port=CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_http_route(self) -> None:
        # The Actions service owns OAuth and bearer verification; no browser forward-auth
        # hop. Keep REST/operator endpoints off this public origin.
        https_route(
            self,
            "httproute",
            metadata=metadata(f"{_NAME}-mcp", self.env.namespace),
            hostname=self.env.actions.hostname,
            backend=_NAME,
            port=CONTAINER_PORT,
            paths=_MCP_PATHS,
            timeout="3600s",
        )

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        cilium.network_policy(
            self,
            "networkpolicy",
            metadata=metadata(_NAME, namespace),
            selector=_LABELS,
            ingress=[
                cilium.ingress_from_gateway(CONTAINER_PORT),
                cilium.ingress_from(cilium.endpoint_labels(namespace, "agentplane-egress"), ports=[CONTAINER_PORT]),
                cilium.ingress_from(cilium.endpoint_labels(namespace, "agentplane-app"), ports=[CONTAINER_PORT]),
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                # Claude's credentialless CIMD document; no wildcard hosts, ports, or redirects.
                cilium.egress_to_fqdns("claude.ai"),
                cilium.egress_to_entities("kube-apiserver"),
                cilium.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    db_constructs.POSTGRES_PORT,
                ),
                *self.env.actions.extra_egress,
            ],
        )

    def _add_pdb(self, min_available: int) -> None:
        k8s.KubePodDisruptionBudget(
            self,
            "pdb",
            metadata=k8s.ObjectMeta(name=_NAME, namespace=self.env.namespace),
            spec=k8s.PodDisruptionBudgetSpec(
                min_available=k8s.IntOrString.from_number(min_available),
                selector=k8s.LabelSelector(match_labels=_LABELS),
            ),
        )
