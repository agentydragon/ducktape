"""The Action Service: its Deployment (+ Alembic migrate initContainer), RBAC, ConfigMaps,
Service, HTTPRoute, NetworkPolicy, and optional PodDisruptionBudget. Environment-only
objects (staging's policy sets, testing's MCP fixtures) come from `Environment.extra`.
"""

from __future__ import annotations

import json
from typing import Any

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    PathMapping,
    Role,
    RoleBinding,
    RolePolicyRule,
    Secret,
    ServiceAccount,
    Volume,
)
from constructs import Construct

from agentplane.action_service.settings import Settings
from agentplane.crd_group import GROUP
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database, notifications
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
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

        props = env.actions
        content: dict[str, Any] = {
            "token_audience": WORKLOAD_TOKEN_AUDIENCE,
            "operator_oidc": props.operator_oidc.model_dump(mode="json", exclude_unset=True),
            "policy_namespace": env.namespace,
            "caller_service_account_namespaces": sorted(props.caller_service_account_namespaces),
            "direct_wait_seconds": 30,
            "max_wait_seconds": 180,
        }
        # These leaves come from the container's environment variables, not the settings file.
        supplied: list[tuple[str, ...]] = [("database_url",)]
        if props.web_push is not None:
            content["web_push"] = {
                "subject": props.web_push.subject,
                "public_base_url": props.web_push.public_base_url,
                "allowed_push_hosts": list(props.web_push.allowed_push_hosts),
            }
            supplied.append(("web_push", "private_key_pem"))
        if props.mcp_client_metadata is not None:
            content["mcp_client_metadata"] = props.mcp_client_metadata.model_dump(mode="json", exclude_unset=True)
        content["mcp_servers"] = {
            key: server.model_dump(mode="json", exclude_unset=True) for key, server in props.mcp_servers.items()
        }
        content["action_groups"] = {
            key: group.model_dump(mode="json", exclude_unset=True) for key, group in props.action_groups.items()
        }
        settings = SettingsFile(
            self,
            "settings",
            metadata=ApiObjectMetadata(name="agentplane-actions-settings", namespace=env.namespace),
            model=Settings,
            content=content,
            path="/etc/agentplane-actions/settings.yaml",
            supplied=supplied,
        )
        workload = self._add_workload(settings)
        self._add_rbac(workload.account)
        self._add_http_route()
        self._add_network_policy()

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
        rules_by_namespace: dict[str, list[RolePolicyRule]] = {
            namespace: [
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource)
                        for resource in ("actionpolicysets", "actionpolicybindings", "policysets", "policybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource)
                        for resource in (
                            "actionpolicysets/status",
                            "actionpolicybindings/status",
                            "policybindings/status",
                        )
                    ],
                    verbs=["patch"],
                ),
            ]
        }
        for caller_namespace in self.env.actions.caller_service_account_namespaces:
            rules_by_namespace.setdefault(caller_namespace, []).append(
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get", "list", "watch"])
            )

        # A `sandbox` ActionGroup stamps Sandboxes, and a `box` group plain Pods from its
        # PodTemplates, owned by the caller's ServiceAccount; both run commands in the Pods. This is
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
                RolePolicyRule(resources=[custom_resource("", "podtemplates")], verbs=["get"]),
                RolePolicyRule(resources=[custom_resource("", "pods")], verbs=["create", "get", "list", "delete"]),
                # `get` and not `create`: kubernetes_asyncio opens exec as an HTTP GET upgrade,
                # where kubectl POSTs.
                RolePolicyRule(resources=[custom_resource("", "pods/exec")], verbs=["get", "create"]),
                # The caller's account, for the owner reference that collects its boxes with it.
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"]),
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
        if self.env.actions.web_push is not None:
            env[env_name(Settings, "web_push", "private_key_pem")] = (
                SecretRef(namespace=namespace, name=self.env.actions.web_push.secret_name)
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

    def _add_workload(self, settings: SettingsFile) -> ServiceWorkload:
        env = self._container_env()
        secret_reload = ",".join([self.mcp_oauth.name, *self.env.actions.extra_reload_secrets])
        workload = add_service_workload(
            self,
            self.env,
            serves=[self.service],
            container="actions",
            image=f"{_ACTIONS_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.ALWAYS,
            args=cli_args(Settings, host="0.0.0.0", port=self.service.pod_port),
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
            # 20s execution drain + 5s forced persistence, with room for HTTP/adapter teardown.
            termination_grace=Duration.seconds(60),
            migrate=migrate_init_container(f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}", name="migrate", env_variables=env),
            annotations={
                "secret.reloader.stakater.com/reload": secret_reload,
                # No configMapGenerator hash rolls the Deployment on settings changes
                # (cluster/docs/cdk8s.md); reloader does.
                "configmap.reloader.stakater.com/reload": settings.config_map.name,
            },
        )
        container = workload.container

        oauth_secret = Secret.from_secret_name(self, "mcp-oauth-secret", self.mcp_oauth.name)
        oauth_volume = Volume.from_secret(
            self,
            "oauth-volume",
            oauth_secret,
            default_mode=0o440,
            items={key: PathMapping(path=key) for key in self.env.actions.oauth_secret_items},
        )
        container.mount("/etc/agentplane-mcp", oauth_volume, read_only=True)
        settings.mount_into(container, env=Settings.config_file_env)
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
            container.mount("/etc/agentplane-github", github_volume, read_only=True)
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
            container.mount(f"/run/secrets/{mount.name}", bearer_volume, read_only=True)
        return workload

    def _add_http_route(self) -> None:
        # The Actions service owns OAuth and bearer verification; no browser forward-auth
        # hop. Keep REST/operator endpoints off this public origin.
        client_metadata_paths = []
        client_metadata = self.env.actions.mcp_client_metadata
        if client_metadata is not None:
            metadata_url = client_metadata.url
            if metadata_url.host != self.env.actions.hostname or metadata_url.port != 443:
                raise ValueError("mcp_client_metadata.url must use this Action Service's HTTPS hostname")
            assert metadata_url.path is not None
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
                database.egress(namespace),
                *self.env.actions.extra_egress,
            ],
        )
