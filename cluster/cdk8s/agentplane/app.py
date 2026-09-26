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
    SandboxTemplateSpecPodTemplateSpecContainers,
    SandboxTemplateSpecPodTemplateSpecContainersEnv,
    SandboxTemplateSpecPodTemplateSpecContainersPorts,
    SandboxTemplateSpecPodTemplateSpecContainersResources,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesLimits,
    SandboxTemplateSpecPodTemplateSpecContainersResourcesRequests,
    SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts,
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
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct

from agentplane.action_service.sandbox.binding import DESCRIPTION_ANNOTATION
from agentplane.app.main import CONFIG_FILE_ENV, Settings
from agentplane.app.oidc import OIDCSettings
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import (
    actions,
    container_security,
    database,
    egress,
    electric,
    llm_ingress,
    node_scheduling,
    sandbox_pod,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo_images import forgejo_images_creds_external_secret, forgejo_images_creds_secret_ref
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.pod_spec_patches import apply_pod_spec_patches
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "agentplane-app"
_APP_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app-migrate"
_RUNNER_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-runner"
CONTAINER_PORT = 8080
_RUNNER_PORT = 7000
_LABELS = {"app.kubernetes.io/name": NAME}
_RUNNER_LABELS = {"app.kubernetes.io/name": "agentplane-runner"}
_CONFIG_DIR = "/etc/agentplane"
# Shared by the runner's --state-dir flag, its container volumeMount, and the
# SandboxTemplate's own VolumeClaimTemplate -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
_STATE_DIR = "/state"


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
            self, "serviceaccount-app", metadata=metadata(NAME, namespace), automount_token=True
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
            service_account_name=NAME,
            namespace=namespace,
        )
        Role(
            self,
            "role",
            metadata=metadata(NAME, namespace),
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
            self, "rolebinding", metadata=metadata(NAME, namespace), role=Role.from_role_name(self, "role-ref", NAME)
        ).add_subjects(app_service_account)

    def _container_env(self) -> dict[str, EnvValue]:
        namespace = self.env.namespace
        postgres_app = Secret.from_secret_name(self, "postgres-app-secret", "postgres-app")
        oidc_secret = Secret.from_secret_name(self, "agentplane-oidc-secret", "agentplane-oidc")
        oidc_session_secret = (
            oidc_secret
            if self.env.app.oidc_session_secret_name == "agentplane-oidc"
            else Secret.from_secret_name(self, "agentplane-oidc-session-secret", self.env.app.oidc_session_secret_name)
        )
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
            env_name(Settings, "electric_url"): EnvValue.from_value(
                f"http://{electric.NAME}.{namespace}.svc.cluster.local:{electric.PORT}"
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
                SecretValue(secret=oidc_session_secret, key="session-secret")
            ),
            env_name(Settings, "token_subjects"): EnvValue.from_value(token_subjects),
        }

    def _add_deployment(self, app_service_account: ServiceAccount) -> Deployment:
        namespace = self.env.namespace
        env = self._container_env()
        oidc_secret_reload = "agentplane-oidc"
        if self.env.app.oidc_session_secret_name != "agentplane-oidc":
            oidc_secret_reload += f",{self.env.app.oidc_session_secret_name}"
        deployment = Deployment(
            self,
            "deployment",
            metadata=metadata(
                NAME,
                namespace,
                labels=_LABELS,
                annotations={
                    # A re-minted client secret otherwise leaves the pod on the old
                    # one, and every login 401s.
                    "secret.reloader.stakater.com/reload": oidc_secret_reload,
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
                port=CONTAINER_PORT,
            ),
            env_variables=env,
            ports=[ContainerPort(name="http", number=CONTAINER_PORT, protocol=Protocol.TCP)],
            readiness=http_probe("/readyz", port=CONTAINER_PORT, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=CONTAINER_PORT, initial_delay_seconds=20, period_seconds=30),
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
            metadata=metadata(NAME, self.env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[ServicePort(name="http", port=CONTAINER_PORT, target_port=CONTAINER_PORT, protocol=Protocol.TCP)],
        )

    def _add_http_route(self) -> None:
        namespace = self.env.namespace
        https_route(
            self,
            "httproute",
            metadata=metadata(namespace, namespace),
            hostname=self.env.app.hostname,
            backend=NAME,
            port=CONTAINER_PORT,
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
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-egress"), egress.PROXY_PORT),
            ],
        )
        # The app takes browser traffic straight from the gateway and reaches DNS, the
        # API server, the OIDC provider, the runner Pods, the egress proxy's admin
        # port, the Action Service, and the trajectory store.
        cilium.network_policy(
            self,
            "networkpolicy-app",
            metadata=metadata(NAME, namespace),
            selector=_LABELS,
            ingress=[cilium.ingress_from_gateway(CONTAINER_PORT)],
            egress=[
                dns_egress,
                cilium.egress_to_entities("kube-apiserver"),
                *self._oidc_egress_rules(),
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-runner"), _RUNNER_PORT),
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-egress"), egress.ADMIN_PORT),
                # Separate BFF/operator transport boundary. The Action Service
                # still requires its own configured operator authenticator;
                # network reachability grants no review authority.
                cilium.egress_to(cilium.endpoint_labels(namespace, "agentplane-actions"), actions.CONTAINER_PORT),
                cilium.egress_to(cilium.endpoint_labels(namespace, electric.NAME), electric.PORT),
                cilium.egress_to(
                    {"k8s:io.kubernetes.pod.namespace": namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
            ],
        )

    def _add_pdb(self, min_available: int) -> None:
        add_pod_disruption_budget(
            self, "pdb", name=NAME, namespace=self.env.namespace, min_available=min_available, selector=_LABELS
        )

    def _runner_container(self) -> SandboxTemplateSpecPodTemplateSpecContainers:
        litellm_url = (
            f"http://agentplane-llm-ingress.{self.env.namespace}.svc.cluster.local:{llm_ingress.CONTAINER_PORT}"
        )
        # The environment a harness child starts from: a bare NAME takes the runner's
        # value, NAME=value sets one. The routing vars are named rather than set, so the
        # container env below is where they are written once and everything in the Pod
        # agrees -- a harness child by this passthrough, anything else by inheritance.
        harness_env = ["HOME", "PATH", *(var.name for var in sandbox_pod.egress_env())]
        args = [
            "--state-dir",
            _STATE_DIR,
            "--listen",
            f"0.0.0.0:{_RUNNER_PORT}",
            # Where the runner image (agentplane/runner/image.nix) links nixpkgs' harnesses.
            "--claude-binary",
            "/bin/claude",
            "--anthropic-base-url",
            "$(LITELLM_URL)",
            "--codex-binary",
            "/bin/codex",
            "--openai-base-url",
            "$(LITELLM_URL)/v1",
        ]
        for entry in harness_env:
            args.extend(["--harness-env", entry])
        return SandboxTemplateSpecPodTemplateSpecContainers(
            name="runner",
            image=f"{_RUNNER_IMAGE}:{_PLACEHOLDER_TAG}",
            args=args,
            # The runner works in absolute paths. This is for a command exec'd in: the sandbox
            # Actions' `runner` boxes start there unless the caller names a directory.
            working_dir=_STATE_DIR,
            ports=[SandboxTemplateSpecPodTemplateSpecContainersPorts(name="runner", container_port=_RUNNER_PORT)],
            security_context=sandbox_pod.workload_security_context(),
            env=[
                SandboxTemplateSpecPodTemplateSpecContainersEnv(name="LITELLM_URL", value=litellm_url),
                # On the container and not just on the harness children the runner spawns.
                *sandbox_pod.egress_env(),
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
                *sandbox_pod.egress_mounts(),
            ],
        )

    def _add_sandbox_template(self) -> None:
        namespace = self.env.namespace
        SandboxTemplate(
            self,
            "sandboxtemplate",
            metadata=metadata(
                "agentplane-runner",
                namespace,
                # What the sandbox Actions tell an agent choosing among the templates they offer.
                annotations={
                    DESCRIPTION_ANNOTATION: (
                        "The shared runner image, built to host an agent harness: the sandbox tools (git, "
                        "curl, ripgrep, jq, openssl, kubectl, python3) plus the runner, Claude Code and Codex."
                    )
                },
            ),
            spec=SandboxTemplateSpec(
                # The CiliumNetworkPolicy next to this construct is the runner's fence.
                network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
                volume_claim_templates_policy=SandboxTemplateSpecVolumeClaimTemplatesPolicy.OVERRIDES,
                pod_template=SandboxTemplateSpecPodTemplate(
                    metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_RUNNER_LABELS),
                    spec=sandbox_pod.pod_spec(
                        self.env, workload=self._runner_container(), service_account_name="agentplane-runner"
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
