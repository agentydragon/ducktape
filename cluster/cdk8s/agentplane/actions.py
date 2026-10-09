"""The Action Service: its Deployment (+ Alembic migrate initContainer), RBAC, ConfigMaps,
Service, HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget. Environment-only
objects (staging's policy sets, testing's MCP fixtures) come from `Environment.extra`.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    PodSecurityContextProps,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    Service,
    ServiceAccount,
    Volume,
)
from constructs import Construct

from agentplane.action_service.main import CONFIG_FILE_ENV, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database, llm_ingress, notifications
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo.images import forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
_NAME = "agentplane-actions"
_ACTIONS_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-action-service"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-action-service-migrate"
_LABELS = {"app.kubernetes.io/name": _NAME}
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


def service(namespace: str) -> ServiceRef:
    """The Action Service in one environment's namespace."""
    return ServiceRef(
        name=_NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def mcp_oauth_secret(namespace: str) -> SecretRef:
    """The MCP OAuth linkage Secret the Action Service reads; in testing, Dex writes it (dex.py)."""
    return SecretRef(namespace=namespace, name="agentplane-mcp-oauth")


class Actions(Construct):
    """ServiceAccount, RBAC, ConfigMaps, Deployment (+ migrate initContainer), Service,
    HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget for the Action Service.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env
        self.service = service(env.namespace)
        self.mcp_oauth = mcp_oauth_secret(env.namespace)

        service_account = self._add_service_account()
        self._add_rbac(service_account)
        # These leaves come from container environment variables, not the settings file.
        supplied: list[tuple[str, ...]] = [("database_url",)]
        if env.actions.web_push_secret_name is not None:
            supplied.append(("web_push", "private_key_pem"))
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name="agentplane-actions-settings", namespace=env.namespace),
            model=Settings,
            content=env.actions.settings.model_dump(mode="json", exclude_unset=True),
            path="/etc/agentplane-actions/settings.yaml",
            supplied=supplied,
        )
        deployment = self._add_deployment(service_account, settings)
        self._add_service(deployment)
        self._add_http_route()
        self._add_network_policy()
        if env.replicas.pdb_min_available is not None:
            self._add_pdb(env.replicas.pdb_min_available)

    def _add_service_account(self) -> ServiceAccount:
        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the Action
        # Service calls TokenReview as itself, so it needs its own mounted token.
        return ServiceAccount(
            self,
            "serviceaccount",
            metadata=ApiObjectMetadata(name=_NAME, namespace=self.env.namespace),
            automount_token=True,
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
        # The runtime policy and caller watch scopes are independent. Keep the policy
        # CR permissions in the policy namespace, and grant only ServiceAccount reads in
        # caller namespaces. No Secrets or serviceaccounts/token permissions are needed.
        policy_namespace = self.env.actions.settings.policy_namespace
        rules_by_namespace: dict[str, list[RolePolicyRule]] = {
            policy_namespace: [
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
            ]
        }
        for caller_namespace in self.env.actions.settings.caller_service_account_namespaces:
            rules_by_namespace.setdefault(caller_namespace, []).append(
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get", "list", "watch"])
            )

        # The sandbox ActionGroup stamps Sandboxes and runs commands in their Pods. This is
        # namespace-wide and cannot say "only the boxes this Action made": that boundary is
        # the executor's own label check (agentplane/action_service/sandbox/inventory.py), which
        # is why it is an application rule tested as one rather than something RBAC states.
        rules_by_namespace.setdefault(namespace, []).extend(
            [
                RolePolicyRule(
                    resources=[custom_resource("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get"]
                ),
                RolePolicyRule(
                    resources=[custom_resource("agents.x-k8s.io", "sandboxes")],
                    verbs=["create", "get", "list", "watch", "delete"],
                ),
                # `get` alone: the Pod backing a Sandbox is named by the controller's
                # `agents.x-k8s.io/pod-name` annotation and read by name, never searched for.
                RolePolicyRule(resources=[custom_resource("", "pods")], verbs=["get"]),
                # `get` and not `create`: kubernetes_asyncio opens exec as an HTTP GET upgrade,
                # where kubectl POSTs.
                RolePolicyRule(resources=[custom_resource("", "pods/exec")], verbs=["get", "create"]),
            ]
        )
        for target_namespace, rules in sorted(rules_by_namespace.items()):
            suffix = "" if target_namespace == namespace else f"-{target_namespace}"
            role = Role(
                self, f"role{suffix}", metadata=ApiObjectMetadata(name=_NAME, namespace=target_namespace), rules=rules
            )
            binding = RoleBinding(
                self,
                f"rolebinding{suffix}",
                metadata=ApiObjectMetadata(name=_NAME, namespace=target_namespace),
                role=role,
            )
            subject = (
                service_account
                if target_namespace == namespace
                else ServiceAccount.from_service_account_name(
                    self, f"service-account-ref-{target_namespace}", _NAME, namespace_name=namespace
                )
            )
            binding.add_subjects(subject)

    def _database_env(self) -> dict[str, EnvValue]:
        # The managed role's login, which database.py mints.
        role = SecretRef(namespace=self.env.namespace, name="postgres-actions")
        return {
            "AGENTPLANE_ACTIONS_DB_USER": role.key("username").env_value(self, "postgres-actions-user-ref"),
            "AGENTPLANE_ACTIONS_DB_PASSWORD": role.key("password").env_value(self, "postgres-actions-password-ref"),
            "AGENTPLANE_ACTIONS_DB_HOST": role.key("host").env_value(self, "postgres-actions-host-ref"),
            "AGENTPLANE_ACTIONS_DB_PORT": role.key("port").env_value(self, "postgres-actions-port-ref"),
            "AGENTPLANE_ACTIONS_DB_NAME": role.key("dbname").env_value(self, "postgres-actions-dbname-ref"),
            env_name(Settings, "database_url"): EnvValue.from_value(
                "postgresql://$(AGENTPLANE_ACTIONS_DB_USER):$(AGENTPLANE_ACTIONS_DB_PASSWORD)"
                "@$(AGENTPLANE_ACTIONS_DB_HOST):$(AGENTPLANE_ACTIONS_DB_PORT)/$(AGENTPLANE_ACTIONS_DB_NAME)"
            ),
        }

    def _container_env(self) -> dict[str, EnvValue]:
        env = self._database_env()
        namespace = self.env.namespace
        env[env_name(Settings, "reader_accounts")] = EnvValue.from_value(
            json.dumps([ServiceAccountRef(namespace=namespace, name=notifications.NAME).model_dump()])
        )
        if self.env.actions.web_push_secret_name is not None:
            env[env_name(Settings, "web_push", "private_key_pem")] = (
                SecretRef(namespace=namespace, name=self.env.actions.web_push_secret_name)
                .key("private-key-pem")
                .env_value(self, "web-push-secret")
            )
        if self.env.actions.github_mcp_client_secret_name is not None:
            env[env_name(Settings, "oauth")] = self.mcp_oauth.key("oauth").env_value(self, "mcp-oauth-secret-env")
            env[env_name(Settings, "mcp_servers", "github", "client_id")] = (
                SecretRef(namespace=namespace, name=self.env.actions.github_mcp_client_secret_name)
                .key("client_id")
                .env_value(self, "github-mcp-client-secret-env")
            )
        return env

    def _add_deployment(self, service_account: ServiceAccount, settings: SettingsFile) -> Deployment:
        namespace = self.env.namespace
        env = self._container_env()
        secret_reload = ",".join([self.mcp_oauth.name, *self.env.actions.extra_reload_secrets])

        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(
                name=_NAME,
                namespace=namespace,
                labels=_LABELS,
                annotations={
                    "secret.reloader.stakater.com/reload": secret_reload,
                    # No configMapGenerator hash rolls the Deployment on settings changes
                    # (cluster/docs/cdk8s.md); reloader does.
                    "configmap.reloader.stakater.com/reload": settings.config_map.name,
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
                Settings, host="0.0.0.0", port=self.service.pod_port, token_audience=llm_ingress.WORKLOAD_TOKEN_AUDIENCE
            ),
            env_variables=env,
            ports=[self.service.port.container_port()],
            readiness=http_probe(
                "/readyz", port=self.service.pod_port, initial_delay_seconds=3, period_seconds=10, timeout_seconds=5
            ),
            liveness=http_probe(
                "/healthz", port=self.service.pod_port, initial_delay_seconds=20, period_seconds=30, timeout_seconds=5
            ),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            # Writable: its root filesystem writes are unaudited.
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )

        oauth_secret = Secret.from_secret_name(self, "mcp-oauth-secret", self.mcp_oauth.name)
        oauth_volume = Volume.from_secret(
            self,
            "oauth-volume",
            oauth_secret,
            default_mode=0o440,
            items={key: PathMapping(path=key) for key in self.env.actions.oauth_secret_items},
        )
        deployment.containers[0].mount("/etc/agentplane-mcp", oauth_volume, read_only=True)
        settings.mount_into(deployment.containers[0], env=CONFIG_FILE_ENV)
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
        for mount in self.env.actions.bearer_mcp_mounts:
            bearer_secret = Secret.from_secret_name(self, f"{mount.name}-bearer-secret", mount.secret_name)
            bearer_volume = Volume.from_secret(
                self,
                f"{mount.name}-bearer-volume",
                bearer_secret,
                items={mount.secret_key: PathMapping(path=mount.file_name)},
                optional=mount.optional or None,
            )
            # Its own directory: a subPath file cannot be mounted inside the read-only
            # settings volume (runc: "not a directory").
            deployment.containers[0].mount(f"/run/secrets/{mount.name}", bearer_volume, read_only=True)

        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=self.service.name, namespace=self.env.namespace),
            selector=deployment,
            ports=[self.service.port.service_port()],
        )

    def _add_http_route(self) -> None:
        # The Actions service owns OAuth and bearer verification; no browser forward-auth
        # hop. Keep REST/operator endpoints off this public origin.
        client_metadata_paths = []
        client_metadata = self.env.actions.settings.mcp_client_metadata
        if client_metadata is not None:
            metadata_url = urlsplit(client_metadata.url)
            if metadata_url.hostname != self.env.actions.hostname or metadata_url.port is not None:
                raise ValueError("mcp_client_metadata.url must use this Action Service's HTTPS hostname")
            client_metadata_paths.append(metadata_url.path)
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=f"{_NAME}-mcp", namespace=self.env.namespace),
            hostnames=[self.env.actions.hostname],
            backend=self.service,
            paths=(*_MCP_PATHS, *sorted(set(client_metadata_paths))),
            timeout="3600s",
        )

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=_NAME, namespace=namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[
                IngressRule.from_gateway(self.service.pod_port),
                notifications.service(namespace).pods.admit(self.service.pod_port),
                # egress.py and app.py import this module, so their Pods are named here.
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-egress"), ports=[self.service.pod_port]
                ),
                IngressRule.from_endpoints(
                    cilium.endpoint_labels(namespace, "agentplane-app"), ports=[self.service.pod_port]
                ),
            ],
            egress=[
                cilium.dns_egress(resolves=["*"]),
                # Claude's credentialless CIMD document; no wildcard hosts, ports, or redirects.
                EgressRule.to_fqdns("claude.ai"),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
                *self.env.actions.extra_egress,
            ],
        )

    def _add_pdb(self, min_available: int) -> None:
        add_pod_disruption_budget(
            self, "pdb", name=_NAME, namespace=self.env.namespace, min_available=min_available, selector=_LABELS
        )
