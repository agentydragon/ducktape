"""The integration app: its Deployment (+ Alembic migrate initContainer), RBAC,
HTTPRoute, NetworkPolicy, the runner SandboxTemplate, and the three ServiceAccounts
(agent/app/runner) involved.

The runner SandboxTemplate's container images carry the same `unset` placeholder tag as
the Deployment's: kustomize's `images:` transformer patches by image name across every
resource in the Kustomization, so image-pins/ covers them too.
"""

from __future__ import annotations

import json
from typing import cast
from urllib.parse import urlsplit

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
    SandboxTemplate,
    SandboxTemplateSpec,
    SandboxTemplateSpecNetworkPolicyManagement,
    SandboxTemplateSpecPodTemplate,
    SandboxTemplateSpecPodTemplateMetadata,
    SandboxTemplateSpecPodTemplateSpec,
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersPorts,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContext,
    SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
    SandboxTemplateSpecPodTemplateSpecImagePullSecrets,
    SandboxTemplateSpecPodTemplateSpecSecurityContext,
    SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile,
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesConfigMap,
    SandboxTemplateSpecPodTemplateSpecVolumesProjected,
    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources,
    SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken,
    SandboxTemplateSpecVolumeClaimTemplates,
    SandboxTemplateSpecVolumeClaimTemplatesMetadata,
    SandboxTemplateSpecVolumeClaimTemplatesPolicy,
    SandboxTemplateSpecVolumeClaimTemplatesSpec,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResources,
    SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests,
)
from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ApiResource,
    ConfigMap,
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    EnvValue,
    IApiResource,
    ImagePullPolicy,
    MemoryResources,
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

from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import (
    actions_constructs,
    container_security,
    db_constructs,
    egress_constructs,
    llm_ingress_constructs,
    node_scheduling,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo_images import (
    SECRET_NAME,
    forgejo_images_creds_external_secret,
    forgejo_images_creds_secret_ref,
)
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name
from x.agentplane.app.main import CONFIG_FILE_ENV, Settings
from x.agentplane.app.oidc import OIDCSettings
from x.agentplane.egress import sidecar

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
# Mounted by the egress sidecar and no other container; every token under it is the Pod's own.
_EGRESS_TOKEN_DIR = "/var/run/agentplane-egress"
# Audiences the central proxy may substitute this Pod's identity for, and the file each is projected
# to under `_EGRESS_TOKEN_DIR`. The volume and the sidecar's mapping are both rendered from this, so
# neither can name a file the other does not project. The hop token is deliberately absent: it
# carries the proxy's own audience, so it is not substitutable anywhere.
_SUBSTITUTABLE_AUDIENCE_FILES = {egress_constructs.KUBERNETES_AUDIENCE: "kubernetes-token"}
_NAME = "agentplane-app"
_APP_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app-migrate"
_RUNNER_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner"
_EGRESS_SIDECAR_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-egress-sidecar"
_CONTAINER_PORT = 8080
_RUNNER_PORT = 7000
_SIDECAR_LISTEN_PORT = 3128
_LABELS = {"app.kubernetes.io/name": _NAME}
_RUNNER_LABELS = {"app.kubernetes.io/name": "agentplane-runner"}
_CONFIG_DIR = "/etc/agentplane"
# Shared by the runner's --state-dir flag, its container volumeMount, and the
# SandboxTemplate's own VolumeClaimTemplate -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
_STATE_DIR = "/state"
# Shared by the runner's egress-ca volumeMount and its pod-level volume -- Kubernetes
# matches the two by this name.
_EGRESS_CA_VOLUME_NAME = "egress-ca"

_MITM_PROXY_URL = f"http://127.0.0.1:{_SIDECAR_LISTEN_PORT}"
_PROXY_VAR_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_NO_PROXY_HOSTS = "127.0.0.1,localhost"
_NO_PROXY_VAR_NAMES = ("NO_PROXY", "no_proxy")
_CA_BUNDLE_PATH = "/etc/ssl/certs/ca-certificates.crt"
_CA_BUNDLE_VAR_NAMES = (
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "REQUESTS_CA_BUNDLE",
)


class App(Construct):
    """ServiceAccounts, RBAC, Deployment (+ migrate initContainer), Service,
    HTTPRoute, NetworkPolicy, optional PodDisruptionBudget, and the runner
    SandboxTemplate.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env

        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=env.namespace)
        app_service_account = self._add_service_accounts()
        self._add_rbac(app_service_account)
        deployment = self._add_deployment(app_service_account)
        self._add_service(deployment)
        self._add_http_route()
        self._add_network_policy()
        if env.replicas.pdb_min_available is not None:
            self._add_pdb(env.replicas.pdb_min_available)
        self._add_sandbox_template()

    def _add_service_accounts(self) -> ServiceAccount:
        namespace = self.env.namespace
        # The identity an agent presents to the app's own API; no Pod runs as it, so no
        # mounted token.
        ServiceAccount(
            self, "serviceaccount-agent", metadata=metadata("agentplane-agent", namespace), automount_token=False
        )
        # cdk8s_plus_34 defaults ServiceAccounts to automount_token=False; the app
        # mounts its own token to call TokenReview as itself.
        app_service_account = ServiceAccount(
            self, "serviceaccount-app", metadata=metadata(_NAME, namespace), automount_token=True
        )
        # The runner Pods' identity, with no RBAC of its own.
        ServiceAccount(
            self, "serviceaccount-runner", metadata=metadata("agentplane-runner", namespace), automount_token=False
        )
        return app_service_account

    def _add_rbac(self, app_service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        # TokenReview proves a Bearer token the app itself was handed. Creating a
        # review grants none of the reviewed identity's authority.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-app-token-reviewer",
            service_account_name=_NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=metadata(_NAME, namespace),
            rules=[
                # GET /sandboxes/templates lists them; a get-only Role 403'd the route (#7023).
                RolePolicyRule(
                    resources=[custom_resource("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get", "list"]
                ),
                RolePolicyRule(
                    resources=[custom_resource("agents.x-k8s.io", "sandboxes")],
                    verbs=["create", "get", "list", "watch", "patch", "delete"],
                ),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                # One ServiceAccount per Sandbox, created with it and owned by it; no
                # patching beyond stamping that owner reference, and no reading of the
                # tokens minted for it.
                RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["create", "patch", "delete"]),
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ("egresspolicies", "egressbindings", "egresscredentials")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "egressbindings")],
                    verbs=["create", "delete"],
                ),
                RolePolicyRule(
                    resources=[
                        custom_resource("agentplane.allegedly.works", resource)
                        for resource in ("actionpolicysets", "actionpolicybindings")
                    ],
                    verbs=["get", "list", "watch"],
                ),
                RolePolicyRule(
                    resources=[custom_resource("agentplane.allegedly.works", "actionpolicybindings")],
                    verbs=["create", "delete"],
                ),
            ],
        )
        RoleBinding(
            self, "rolebinding", metadata=metadata(_NAME, namespace), role=Role.from_role_name(self, "role-ref", _NAME)
        ).add_subjects(app_service_account)

    def _container_env(self) -> dict[str, EnvValue]:
        namespace = self.env.namespace
        postgres_app = Secret.from_secret_name(self, "postgres-app-secret", "postgres-app")
        oidc_secret = Secret.from_secret_name(self, "agentplane-oidc-secret", "agentplane-oidc")
        token_subjects = json.dumps([f"system:serviceaccount:{namespace}:agentplane-agent"])
        return {
            CONFIG_FILE_ENV: EnvValue.from_value(f"{_CONFIG_DIR}/config.yaml"),
            "AGENTPLANE_DB_USER": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="username")),
            "AGENTPLANE_DB_PASSWORD": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="password")),
            "AGENTPLANE_DB_HOST": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="host")),
            "AGENTPLANE_DB_PORT": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="port")),
            "AGENTPLANE_DB_NAME": EnvValue.from_secret_value(SecretValue(secret=postgres_app, key="dbname")),
            env_name(Settings, "database_url"): EnvValue.from_value(
                "postgresql+asyncpg://$(AGENTPLANE_DB_USER):$(AGENTPLANE_DB_PASSWORD)"
                "@$(AGENTPLANE_DB_HOST):$(AGENTPLANE_DB_PORT)/$(AGENTPLANE_DB_NAME)"
            ),
            env_name(OIDCSettings, "issuer"): EnvValue.from_value(self.env.app.oidc_issuer),
            env_name(OIDCSettings, "public_base_url"): EnvValue.from_value(f"https://{self.env.app.hostname}"),
            env_name(OIDCSettings, "client_id"): EnvValue.from_secret_value(
                SecretValue(secret=oidc_secret, key="client-id")
            ),
            env_name(OIDCSettings, "client_secret"): EnvValue.from_secret_value(
                SecretValue(secret=oidc_secret, key="client-secret")
            ),
            env_name(OIDCSettings, "session_secret"): EnvValue.from_secret_value(
                SecretValue(secret=oidc_secret, key="session-secret")
            ),
            env_name(Settings, "token_subjects"): EnvValue.from_value(token_subjects),
        }

    def _add_deployment(self, app_service_account: ServiceAccount) -> Deployment:
        namespace = self.env.namespace
        env = self._container_env()
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                _NAME,
                namespace,
                labels=_LABELS,
                annotations={
                    # A re-minted client secret otherwise leaves the pod on the old
                    # one, and every login 401s.
                    "secret.reloader.stakater.com/reload": "agentplane-oidc",
                    "configmap.reloader.stakater.com/reload": "agentplane-app-config",
                },
            ),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=self.env.replicas.count,
            strategy=self.env.replicas.strategy,
            min_ready=self.env.replicas.min_ready,
            # 5s HTTP/SSE drain (--shutdown-timeout), with room for the bridge's lease
            # release and the store's close.
            termination_grace_period=Duration.seconds(60),
            service_account=app_service_account,
            # cdk8s_plus_34 defaults this to False independent of the ServiceAccount's
            # own automount_token -- opt in for the same reason as the ServiceAccount.
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "forgejo-images-creds-ref"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            # The app's Alembic history. The app itself creates no tables; it verifies
            # the migrated schema at startup and fails if this hasn't run.
            init_containers=[migrate_init_container(f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}", env_variables=env)],
        )
        deployment.add_container(
            name="app",
            image=f"{_APP_IMAGE}:{_PLACEHOLDER_TAG}",
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            args=cli_args(
                Settings,
                namespace=namespace,
                # The same namespace for now: the split the flag exists for is a
                # separate change, which moves the Sandboxes, their template, and the
                # runner ServiceAccount out of here.
                sandbox_namespace=namespace,
                runner_port=_RUNNER_PORT,
                host="0.0.0.0",
                port=_CONTAINER_PORT,
            ),
            env_variables=env,
            ports=[ContainerPort(name="http", number=_CONTAINER_PORT, protocol=Protocol.TCP)],
            readiness=http_probe("/readyz", port=_CONTAINER_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=_CONTAINER_PORT, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=container_security.WRITABLE_ROOT,
        )
        config = ConfigMap.from_config_map_name(self, "app-config-ref", "agentplane-app-config")
        volume = Volume.from_config_map(self, "config-volume", config)
        deployment.containers[0].mount(_CONFIG_DIR, volume, read_only=True)

        # With the database (cnpg_conventions R5). Unlike llm-ingress/egress, the app
        # carries no control-plane toleration.
        node_scheduling.attract_to_zone(deployment)
        apply_pod_spec_patches(deployment)
        return deployment

    def _add_service(self, deployment: Deployment) -> None:
        Service(
            self,
            "service",
            metadata=metadata(_NAME, self.env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[ServicePort(name="http", port=_CONTAINER_PORT, target_port=_CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_http_route(self) -> None:
        namespace = self.env.namespace
        https_route(
            self,
            "httproute",
            metadata=metadata(namespace, namespace),
            hostname=self.env.app.hostname,
            backend=_NAME,
            port=_CONTAINER_PORT,
            # A session stream stays attached for as long as the tab is open.
            timeout="3600s",
        )

    def _oidc_egress_rules(self) -> list[CiliumNetworkPolicySpecEgress]:
        server_name = urlsplit(self.env.app.oidc_issuer).hostname
        assert server_name is not None, f"OIDC issuer has no hostname: {self.env.app.oidc_issuer!r}"
        rules = [cilium.egress_via_gateway(server_name)]
        if self.env.app.reach_incluster_authentik:
            rules.append(cilium.egress_to(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=[server_name]))
        return rules

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        dns_egress = cilium.dns_egress()
        # Runner Pods reach DNS and the egress proxy's listener, which the sidecar
        # relays to; port 7000 is open only to Pods in this namespace.
        cilium.network_policy(
            self,
            "networkpolicy-runner",
            metadata=metadata("agentplane-runner", namespace),
            selector=_RUNNER_LABELS,
            ingress=[cilium.ingress_from({"k8s:io.kubernetes.pod.namespace": namespace}, ports=[_RUNNER_PORT])],
            egress=[
                dns_egress,
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-egress"), egress_constructs.PROXY_PORT),
            ],
        )
        # The app takes browser traffic straight from the gateway and reaches DNS, the
        # API server, the OIDC provider, the runner Pods, the egress proxy's admin
        # port, the Action Service, and the trajectory store.
        cilium.network_policy(
            self,
            "networkpolicy-app",
            metadata=metadata(_NAME, namespace),
            selector=_LABELS,
            ingress=[cilium.ingress_from_gateway(_CONTAINER_PORT)],
            egress=[
                dns_egress,
                cilium.egress_to_entities("kube-apiserver"),
                *self._oidc_egress_rules(),
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-runner"), _RUNNER_PORT),
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-egress"), egress_constructs.ADMIN_PORT),
                # Separate BFF/operator transport boundary. The Action Service
                # still requires its own configured operator authenticator;
                # network reachability grants no review authority.
                cilium.egress_to(
                    cilium.endpoint_labels(namespace, "agentplane-actions"), actions_constructs.CONTAINER_PORT
                ),
                cilium.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    db_constructs.POSTGRES_PORT,
                ),
            ],
        )

    def _add_pdb(self, min_available: int) -> None:
        add_pod_disruption_budget(
            self,
            "pdb",
            name=_NAME,
            namespace=self.env.namespace,
            min_available=min_available,
            selector=_LABELS,
        )

    def _runner_container(self) -> SandboxTemplateSpecPodTemplateSpecContainers:
        litellm_url = (
            f"http://agentplane-llm-ingress.{self.env.namespace}"
            f".svc.cluster.local:{llm_ingress_constructs.CONTAINER_PORT}"
        )
        # The environment a harness child starts from: a bare NAME takes the runner's
        # value, NAME=value sets one. Both spellings of proxy vars, since clients
        # disagree on case; NO_PROXY is loopback and nothing else.
        harness_env = [
            "HOME",
            "PATH",
            *(f"{name}={_MITM_PROXY_URL}" for name in _PROXY_VAR_NAMES),
            *(f"{name}={_NO_PROXY_HOSTS}" for name in _NO_PROXY_VAR_NAMES),
            *(f"{name}={_CA_BUNDLE_PATH}" for name in _CA_BUNDLE_VAR_NAMES),
        ]
        args = [
            "--state-dir",
            _STATE_DIR,
            "--listen",
            f"0.0.0.0:{_RUNNER_PORT}",
            "--claude-binary",
            "/usr/local/bin/claude",
            "--anthropic-base-url",
            "$(LITELLM_URL)",
            "--codex-binary",
            "/opt/codex/bin/codex",
            "--openai-base-url",
            "$(LITELLM_URL)/v1",
        ]
        for entry in harness_env:
            args.extend(["--harness-env", entry])
        return SandboxTemplateSpecPodTemplateSpecContainers(
            name="runner",
            image=f"{_RUNNER_IMAGE}:{_PLACEHOLDER_TAG}",
            args=args,
            ports=[SandboxTemplateSpecPodTemplateSpecContainersPorts(name="runner", container_port=_RUNNER_PORT)],
            security_context=SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
                allow_privilege_escalation=False,
                capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(drop=["ALL"]),
            ),
            env=[
                SandboxTemplateSpecPodTemplateSpecContainersEnv(name="LITELLM_URL", value=litellm_url),
                # Neither a workload token nor a LiteLLM key: the placeholder the
                # agentplane-workload EgressCredential derives from its name. Central
                # substitutes the sidecar-only projected token; the ingress replaces
                # that with its server-held LiteLLM virtual key after live
                # WorkloadPrincipal resolution.
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="ANTHROPIC_AUTH_TOKEN", value="agentplane-credential-agentplane-workload"
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name="OPENAI_API_KEY", value="agentplane-credential-agentplane-workload"
                ),
            ],
            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                requests={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("500m"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("1Gi"),
                },
                limits={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("2"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("4Gi"),
                },
            ),
            volume_mounts=[
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                    name=_STATE_VOLUME_NAME, mount_path=_STATE_DIR
                ),
                # Public roots + cluster root + the proxy's interception root, over
                # the image's own bundle at the path every client falls back to. A
                # subPath mount does not follow ConfigMap updates: a CA rotation
                # reaches a sandbox at its next Pod.
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                    name=_EGRESS_CA_VOLUME_NAME,
                    mount_path=_CA_BUNDLE_PATH,
                    sub_path=egress_constructs.CA_BUNDLE_KEY,
                    read_only=True,
                ),
            ],
        )

    def _egress_sidecar_container(self) -> SandboxTemplateSpecPodTemplateSpecContainers:
        namespace = self.env.namespace
        return SandboxTemplateSpecPodTemplateSpecContainers(
            name="egress-sidecar",
            image=f"{_EGRESS_SIDECAR_IMAGE}:{_PLACEHOLDER_TAG}",
            env=[
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name=env_name(sidecar.Settings, "proxy_host"),
                    value=f"agentplane-egress.{namespace}.svc.cluster.local",
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name=env_name(sidecar.Settings, "proxy_port"), value=str(egress_constructs.PROXY_PORT)
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name=env_name(sidecar.Settings, "listen_port"), value=str(_SIDECAR_LISTEN_PORT)
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name=env_name(sidecar.Settings, "token_file"), value=f"{_EGRESS_TOKEN_DIR}/token"
                ),
                SandboxTemplateSpecPodTemplateSpecContainersEnv(
                    name=env_name(sidecar.Settings, "audience_token_files"),
                    value=json.dumps(
                        {
                            audience: f"{_EGRESS_TOKEN_DIR}/{file}"
                            for audience, file in _SUBSTITUTABLE_AUDIENCE_FILES.items()
                        }
                    ),
                ),
            ],
            security_context=SandboxTemplateSpecPodTemplateSpecContainersSecurityContext(
                allow_privilege_escalation=False,
                capabilities=SandboxTemplateSpecPodTemplateSpecContainersSecurityContextCapabilities(drop=["ALL"]),
            ),
            resources=SandboxTemplateSpecPodTemplateSpecContainersResources(
                requests={
                    "cpu": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("10m"),
                    "memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests.from_string("32Mi"),
                },
                limits={"memory": SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits.from_string("128Mi")},
            ),
            volume_mounts=[
                SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                    name="egress-token", mount_path=_EGRESS_TOKEN_DIR, read_only=True
                )
            ],
        )

    def _add_sandbox_template(self) -> None:
        namespace = self.env.namespace
        node_selector = (
            {"topology.kubernetes.io/zone": self.env.app.runner_zone} if self.env.app.runner_zone is not None else None
        )

        SandboxTemplate(
            self,
            "sandboxtemplate",
            metadata=metadata("agentplane-runner", namespace),
            spec=SandboxTemplateSpec(
                # The CiliumNetworkPolicy next to this construct is the runner's fence.
                network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
                volume_claim_templates_policy=SandboxTemplateSpecVolumeClaimTemplatesPolicy.OVERRIDES,
                pod_template=SandboxTemplateSpecPodTemplate(
                    metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_RUNNER_LABELS),
                    spec=SandboxTemplateSpecPodTemplateSpec(
                        containers=[self._runner_container(), self._egress_sidecar_container()],
                        automount_service_account_token=False,
                        image_pull_secrets=[SandboxTemplateSpecPodTemplateSpecImagePullSecrets(name=SECRET_NAME)],
                        # With the rest of the namespace and with LiteLLM: a runner's
                        # model calls and its hop to the app both stay inside the zone.
                        node_selector=node_selector,
                        service_account_name="agentplane-runner",
                        termination_grace_period_seconds=60,
                        security_context=SandboxTemplateSpecPodTemplateSpecSecurityContext(
                            run_as_non_root=True,
                            run_as_user=1000,
                            run_as_group=1000,
                            fs_group=1000,
                            seccomp_profile=SandboxTemplateSpecPodTemplateSpecSecurityContextSeccompProfile(
                                type="RuntimeDefault"
                            ),
                        ),
                        volumes=[
                            SandboxTemplateSpecPodTemplateSpecVolumes(
                                name=_EGRESS_CA_VOLUME_NAME,
                                config_map=SandboxTemplateSpecPodTemplateSpecVolumesConfigMap(
                                    name=self.env.egress.ca_secret_name
                                ),
                            ),
                            # The Pod's identity, and to nobody else: this volume is mounted by
                            # the egress sidecar alone, so no token here is readable from the
                            # container an agent runs commands in. `token` proves the Pod to the
                            # central proxy; each of the rest is the same account minted for a
                            # destination's own audience, which the proxy substitutes where a rule
                            # names that audience and which is useless at the proxy itself. All are
                            # bound to this Pod and rotated by kubelet.
                            SandboxTemplateSpecPodTemplateSpecVolumes(
                                name="egress-token",
                                projected=SandboxTemplateSpecPodTemplateSpecVolumesProjected(
                                    sources=[
                                        SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources(
                                            service_account_token=SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken(
                                                audience=llm_ingress_constructs.WORKLOAD_TOKEN_AUDIENCE,
                                                expiration_seconds=600,
                                                path="token",
                                            )
                                        ),
                                        *(
                                            SandboxTemplateSpecPodTemplateSpecVolumesProjectedSources(
                                                service_account_token=SandboxTemplateSpecPodTemplateSpecVolumesProjectedSourcesServiceAccountToken(
                                                    audience=audience, expiration_seconds=600, path=file
                                                )
                                            )
                                            for audience, file in _SUBSTITUTABLE_AUDIENCE_FILES.items()
                                        ),
                                    ]
                                ),
                            ),
                        ],
                    ),
                ),
                volume_claim_templates=[
                    SandboxTemplateSpecVolumeClaimTemplates(
                        metadata=SandboxTemplateSpecVolumeClaimTemplatesMetadata(name=_STATE_VOLUME_NAME),
                        spec=SandboxTemplateSpecVolumeClaimTemplatesSpec(
                            # The bulk tier, and the only one a sandbox can have: OVH's
                            # `tier=ssd` nodes are control-plane, and a sandbox is not
                            # getting that toleration. Node-local, so a sandbox does
                            # not outlive its node -- which is what a sandbox is for.
                            storage_class_name="local-path-ovh-hdd",
                            access_modes=["ReadWriteOnce"],
                            resources=SandboxTemplateSpecVolumeClaimTemplatesSpecResources(
                                requests={
                                    "storage": SandboxTemplateSpecVolumeClaimTemplatesSpecResourcesRequests.from_string(
                                        "10Gi"
                                    )
                                }
                            ),
                        ),
                    )
                ],
            ),
        )
