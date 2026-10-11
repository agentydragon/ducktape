"""The integration app: its Deployment (+ Alembic migrate initContainer), RBAC,
HTTPRoute, NetworkPolicy, the runner SandboxTemplate, and the three ServiceAccounts
(agent/app/runner) involved.

The runner SandboxTemplate's container images carry the same `unset` placeholder tag as
the Deployment's: kustomize's `images:` transformer patches by image name across every
resource in the Kustomization, so image-pins/ covers them too.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import cast
from urllib.parse import urlsplit

from agent_sandbox_sandboxtemplate_crds.io.x_k8s.agents.extensions import (
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
    SandboxTemplateSpecPodTemplateSpecVolumes,
    SandboxTemplateSpecPodTemplateSpecVolumesSecret,
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
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    IApiResource,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress
from constructs import Construct

from agentplane.action_service.sandbox.binding import DESCRIPTION_ANNOTATION
from agentplane.app.oidc import OIDCSettings
from agentplane.app.settings import CONFIG_FILE_ENV, Settings, ThreadReads
from agentplane.crd_group import GROUP
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import (
    actions,
    database,
    egress,
    electric,
    history_service,
    llm_ingress,
    notifications,
    runner_gateway,
    sandbox_pod,
    sandbox_service,
)
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import ServiceWorkload, add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.agent_sandbox.sandbox_template import SandboxTemplate
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import cli_args, env_name

_PLACEHOLDER_TAG = "unset"  # always overridden by image-pins/kustomization.yaml
NAME = "agentplane-app"
_APP_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app"
_MIGRATE_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-app-migrate"
RUNNER_IMAGE = "git.allegedly.works/ducktape-ci/runner"
_LABELS = {"app.kubernetes.io/name": NAME}
_RUNNER_LABELS = {"app.kubernetes.io/name": "agentplane-runner"}
# Shared by the runner's --state-dir flag, its container volumeMount, and the
# SandboxTemplate's own VolumeClaimTemplate -- all three must name the same volume.
_STATE_VOLUME_NAME = "state"
_STATE_DIR = "/state"
_BUILDBUDDY_SECRET = "buildbuddy-api-key"
_BUILDBUDDY_MOUNT = "/run/buildbuddy"


def service(namespace: str) -> ServiceRef:
    """The app's Service in one environment's namespace."""
    return ServiceRef(
        name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def oidc_secret(namespace: str) -> SecretRef:
    """The OIDC client Secret the app reads; in testing, Dex writes it (dex.py)."""
    return SecretRef(namespace=namespace, name="agentplane-oidc")


class App(Construct):
    """ServiceAccounts, RBAC, the config ConfigMap, Deployment (+ migrate initContainer), Service,
    HTTPRoute, NetworkPolicy, optional PodDisruptionBudget, and the runner
    SandboxTemplate.
    """

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env
        self.service = service(env.namespace)
        self.oidc = oidc_secret(env.namespace)

        forgejo_images_creds_external_secret(self, "forgejo-images-creds", namespace=env.namespace)
        self._add_service_accounts()
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name="agentplane-app-config", namespace=env.namespace),
            model=Settings,
            content=env.app_config.model_dump(mode="json", exclude_unset=True),
            path="/etc/agentplane/config.yaml",
        )
        workload = self._add_workload(config)
        self._add_rbac(workload.account)
        self._add_http_route()
        self._add_network_policy()
        RunnerTemplate(
            self,
            "runner-template",
            env,
            name="runner",
            image=RUNNER_IMAGE,
            description=(
                "The shared runner image, built to host an agent harness: the sandbox tools (git, "
                "curl, ripgrep, jq, openssl, kubectl, python3) plus the runner, Claude Code and Codex."
            ),
        )

    def _add_service_accounts(self) -> None:
        namespace = self.env.namespace
        # The identity an agent presents to the app's own API; no Pod runs as it, so no
        # mounted token.
        ServiceAccount(
            self,
            "serviceaccount-agent",
            metadata=ApiObjectMetadata(name="agentplane-agent", namespace=namespace),
            automount_token=False,
        )
        # The runner Pods' identity, with no RBAC of its own.
        ServiceAccount(
            self,
            "serviceaccount-runner",
            metadata=ApiObjectMetadata(name="agentplane-runner", namespace=namespace),
            automount_token=False,
        )

    def _add_rbac(self, app_service_account: ServiceAccount) -> None:
        namespace = self.env.namespace
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{namespace}-app-token-reviewer",
            service_account_name=NAME,
            namespace=namespace,
        )
        role = Role(
            self,
            "role",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            rules=[
                RolePolicyRule(
                    resources=[custom_resource("extensions.agents.x-k8s.io", "sandboxtemplates")], verbs=["get", "list"]
                ),
                RolePolicyRule(
                    resources=[custom_resource("agents.x-k8s.io", "sandboxes")], verbs=["get", "list", "watch"]
                ),
                RolePolicyRule(resources=[cast(IApiResource, ApiResource.PODS)], verbs=["get", "list", "watch"]),
                RolePolicyRule(
                    resources=[
                        custom_resource(GROUP, resource)
                        for resource in (
                            "egresspolicies",
                            "egressbindings",
                            "egresscredentials",
                            "actionpolicysets",
                            "actionpolicybindings",
                        )
                    ],
                    verbs=["get", "list", "watch"],
                ),
            ],
        )
        RoleBinding(
            self, "rolebinding", metadata=ApiObjectMetadata(name=NAME, namespace=namespace), role=role
        ).add_subjects(app_service_account)

    def _container_env(self) -> dict[str, EnvValue]:
        namespace = self.env.namespace
        postgres_app = database.postgres(self.env).app_secret
        token_subjects = json.dumps([f"system:serviceaccount:{namespace}:agentplane-agent"])
        return {
            "AGENTPLANE_DB_USER": postgres_app.key("username").env_value(self, "postgres-app-user-ref"),
            "AGENTPLANE_DB_PASSWORD": postgres_app.key("password").env_value(self, "postgres-app-password-ref"),
            "AGENTPLANE_DB_HOST": postgres_app.key("host").env_value(self, "postgres-app-host-ref"),
            "AGENTPLANE_DB_PORT": postgres_app.key("port").env_value(self, "postgres-app-port-ref"),
            "AGENTPLANE_DB_NAME": postgres_app.key("dbname").env_value(self, "postgres-app-dbname-ref"),
            env_name(Settings, "database_url"): EnvValue.from_value(
                "postgresql+asyncpg://$(AGENTPLANE_DB_USER):$(AGENTPLANE_DB_PASSWORD)"
                "@$(AGENTPLANE_DB_HOST):$(AGENTPLANE_DB_PORT)/$(AGENTPLANE_DB_NAME)"
            ),
            env_name(Settings, "electric_url"): EnvValue.from_value(
                electric.service(
                    namespace,
                    electric.HISTORY if self.env.thread_reads is ThreadReads.HISTORY_SERVICE else electric.APP,
                ).url
            ),
            env_name(Settings, "thread_reads"): EnvValue.from_value(self.env.thread_reads),
            env_name(OIDCSettings, "issuer"): EnvValue.from_value(self.env.app.oidc_issuer),
            env_name(OIDCSettings, "public_base_url"): EnvValue.from_value(f"https://{self.env.app.hostname}"),
            env_name(OIDCSettings, "client_id"): self.oidc.key("client-id").env_value(self, "oidc-client-id-ref"),
            env_name(OIDCSettings, "client_secret"): self.oidc.key("client-secret").env_value(
                self, "oidc-client-secret-ref"
            ),
            env_name(OIDCSettings, "session_secret"): SecretRef(
                namespace=namespace, name=self.env.app.oidc_session_secret_name
            )
            .key("session-secret")
            .env_value(self, "oidc-session-secret-ref"),
            env_name(Settings, "token_subjects"): EnvValue.from_value(token_subjects),
        }

    def _add_workload(self, config: SettingsFile) -> ServiceWorkload:
        namespace = self.env.namespace
        env = self._container_env()
        oidc_secret_reload = self.oidc.name
        if self.env.app.oidc_session_secret_name != self.oidc.name:
            oidc_secret_reload += f",{self.env.app.oidc_session_secret_name}"
        workload = add_service_workload(
            self,
            self.env,
            serves=[self.service],
            container="app",
            image=f"{_APP_IMAGE}:{_PLACEHOLDER_TAG}",
            args=cli_args(
                Settings,
                namespace=namespace,
                # The same namespace for now: the split the flag exists for is a
                # separate change, which moves the Sandboxes, their template, and the
                # runner ServiceAccount out of here.
                sandbox_namespace=namespace,
                sandbox_service_target=f"{sandbox_service.service(namespace).fqdn}:{sandbox_service.service(namespace).pod_port}",
                runner_gateway_target=runner_gateway.target(namespace),
                sandbox_service_control_target=runner_gateway.control_target(namespace),
                history_service_target=f"{history_service.service(namespace).fqdn}:{history_service.service(namespace).pod_port}",
                notifications_url=f"http://{notifications.service(namespace).fqdn}:{notifications.service(namespace).port.number}",
                notifications_token_file="/var/run/secrets/agentplane-notifications/token",
                host="0.0.0.0",
                port=self.service.pod_port,
            ),
            env_variables=env,
            ports=[self.service.port.container_port()],
            readiness=http_probe("/readyz", port=self.service.pod_port, initial_delay_seconds=3, period_seconds=10),
            liveness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(50)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            # 5s HTTP/SSE drain (--shutdown-timeout), with room for the bridge's lease
            # release and the store's close.
            termination_grace=Duration.seconds(60),
            # The app's Alembic history. The app itself creates no tables; it verifies
            # the migrated schema at startup and fails if this hasn't run.
            migrate=migrate_init_container(f"{_MIGRATE_IMAGE}:{_PLACEHOLDER_TAG}", name="migrate", env_variables=env),
            annotations={
                # A re-minted client secret otherwise leaves the pod on the old
                # one, and every login 401s.
                "secret.reloader.stakater.com/reload": oidc_secret_reload,
                "configmap.reloader.stakater.com/reload": config.config_map.name,
            },
        )
        config.mount_into(workload.container, env=CONFIG_FILE_ENV)
        for volume, audience, mount_path in [
            ("sandbox-service-token", sandbox_service.TOKEN_AUDIENCE, "/var/run/secrets/agentplane-sandbox-service"),
            ("history-service-token", history_service.TOKEN_AUDIENCE, "/var/run/secrets/agentplane-history-service"),
            ("notifications-token", notifications.TOKEN_AUDIENCE, "/var/run/secrets/agentplane-notifications"),
        ]:
            workload.project_tokens(volume, mount_path, {"token": audience})
        return workload

    def _add_http_route(self) -> None:
        namespace = self.env.namespace
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=namespace, namespace=namespace),
            hostnames=[self.env.app.hostname],
            backend=self.service,
            # A session stream stays attached for as long as the tab is open.
            timeout="3600s",
        )

    def _oidc_egress_rules(self) -> list[CiliumNetworkPolicySpecEgress]:
        server_name = urlsplit(self.env.app.oidc_issuer).hostname
        assert server_name is not None, f"OIDC issuer has no hostname: {self.env.app.oidc_issuer!r}"
        rules = [cilium.egress_via_gateway(server_name)]
        if self.env.app.reach_incluster_authentik:
            rules.append(EgressRule.to_endpoints(cilium.AUTHENTIK_SERVER_LABELS, 9000, server_names=[server_name]))
        return rules

    def _add_network_policy(self) -> None:
        namespace = self.env.namespace
        runner = Pods(namespace=namespace, labels=tuple(_RUNNER_LABELS.items()))
        dns_egress = cilium.dns_egress()
        # Runner Pods reach DNS and the egress proxy's listener, which the sidecar
        # relays to; only the Runner Gateway can connect to the control port.
        # TODO: replace network-only runner authentication with authenticated transport.
        NetworkPolicy(
            self,
            "networkpolicy-runner",
            metadata=ApiObjectMetadata(name="agentplane-runner", namespace=namespace),
            endpoint_selector=runner.selector,
            ingress=[runner_gateway.service(namespace).pods.admit(runner_gateway.RUNNER_PORT)],
            egress=[dns_egress, egress.proxy(namespace).egress()],
        )
        # The app takes browser traffic straight from the gateway and reaches DNS, the
        # API server, the OIDC provider, Sandbox Service, Runner Gateway, History Service, the egress
        # proxy's admin port, the Action Service, and the trajectory store.
        NetworkPolicy(
            self,
            "networkpolicy-app",
            metadata=ApiObjectMetadata(name=NAME, namespace=namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[IngressRule.from_gateway(self.service.pod_port)],
            egress=[
                dns_egress,
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                *self._oidc_egress_rules(),
                sandbox_service.service(namespace).egress(),
                runner_gateway.service(namespace).egress(),
                history_service.service(namespace).egress(),
                notifications.service(namespace).egress(),
                egress.admin(namespace).egress(),
                # Separate BFF/operator transport boundary. The Action Service
                # still requires its own configured operator authenticator;
                # network reachability grants no review authority.
                actions.service(namespace).egress(),
                electric.service(namespace, electric.APP).egress(),
                electric.service(namespace, electric.HISTORY).egress(),
                database.egress(namespace),
            ],
        )


class RunnerTemplate(Construct):
    """Shared runner Pod and storage wiring; staging ducktape may opt into a direct BuildBuddy key."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        name: str,
        image: str,
        description: str,
        buildbuddy_secret: bool = False,
        extra_harness_env: Sequence[str] = (),
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.buildbuddy_secret = buildbuddy_secret
        self.extra_harness_env = extra_harness_env
        self._add_sandbox_template(name=name, image=image, description=description)

    def _runner_container(self, image: str) -> SandboxTemplateSpecPodTemplateSpecContainers:
        # Through the egress proxy, which matches the host on the exact string its policy names.
        llm = llm_ingress.service(self.env.namespace)
        litellm_url = f"http://{llm.fqdn}:{llm.port.number}"
        # The environment a harness child starts from: a bare NAME takes the runner's
        # value, NAME=value sets one. The routing vars are named rather than set, so the
        # container env below is where they are written once and everything in the Pod
        # agrees -- a harness child by this passthrough, anything else by inheritance.
        # Deployment-wide workload defaults share the same allowlist and are added by
        # sandbox_pod.workload_environment when the container is built. Names the image owns are
        # listed separately, on --harness-inherit-env, so this invocation forwards them without
        # repeating a value the deployment has no business choosing.
        harness_env = list(
            dict.fromkeys(
                [
                    "HOME",
                    "PATH",
                    *self.env.sandbox_workload_env,
                    *(var.name for var in sandbox_pod.egress_env()),
                    *self.extra_harness_env,
                ]
            )
        )
        args = [
            "--state-dir",
            _STATE_DIR,
            "--listen",
            f"0.0.0.0:{runner_gateway.RUNNER_PORT}",
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
        for name in self.env.harness_inherited_env:
            args.extend(["--harness-inherit-env", name])
        return SandboxTemplateSpecPodTemplateSpecContainers(
            name="runner",
            image=f"{image}:{_PLACEHOLDER_TAG}",
            args=args,
            # The runner works in absolute paths. This is for a command exec'd in: the sandbox
            # Actions' `runner` boxes start there unless the caller names a directory.
            working_dir=_STATE_DIR,
            ports=[
                SandboxTemplateSpecPodTemplateSpecContainersPorts(
                    name="runner", container_port=runner_gateway.RUNNER_PORT
                )
            ],
            security_context=sandbox_pod.workload_security_context(),
            env=sandbox_pod.workload_environment(
                self.env,
                [
                    SandboxTemplateSpecPodTemplateSpecContainersEnv(name="LITELLM_URL", value=litellm_url),
                    *(
                        [
                            SandboxTemplateSpecPodTemplateSpecContainersEnv(
                                name="BBR_BUILDBUDDY_API_KEY_FILE", value=f"{_BUILDBUDDY_MOUNT}/api-key"
                            )
                        ]
                        if self.buildbuddy_secret
                        else []
                    ),
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
            ),
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
                *(
                    [
                        SandboxTemplateSpecPodTemplateSpecContainersVolumeMounts(
                            name=_BUILDBUDDY_SECRET, mount_path=_BUILDBUDDY_MOUNT, read_only=True
                        )
                    ]
                    if self.buildbuddy_secret
                    else []
                ),
            ],
        )

    def _add_sandbox_template(self, *, name: str, image: str, description: str) -> None:
        namespace = self.env.namespace
        SandboxTemplate(
            self,
            "sandboxtemplate",
            metadata=ApiObjectMetadata(
                name=name,
                namespace=namespace,
                # What the sandbox Actions tell an agent choosing among the templates they offer.
                annotations={DESCRIPTION_ANNOTATION: description},
            ),
            # The CiliumNetworkPolicy next to this construct is the runner's fence.
            network_policy_management=SandboxTemplateSpecNetworkPolicyManagement.UNMANAGED,
            volume_claim_templates_policy=SandboxTemplateSpecVolumeClaimTemplatesPolicy.OVERRIDES,
            pod_template=SandboxTemplateSpecPodTemplate(
                metadata=SandboxTemplateSpecPodTemplateMetadata(labels=_RUNNER_LABELS),
                spec=sandbox_pod.pod_spec(
                    self.env,
                    workload=self._runner_container(image),
                    service_account_name="agentplane-runner",
                    workload_volumes=(
                        [
                            SandboxTemplateSpecPodTemplateSpecVolumes(
                                name=_BUILDBUDDY_SECRET,
                                secret=SandboxTemplateSpecPodTemplateSpecVolumesSecret(
                                    secret_name=_BUILDBUDDY_SECRET,
                                    default_mode=288,  # 0440; readable via fsGroup 1000
                                ),
                            )
                        ]
                        if self.buildbuddy_secret
                        else []
                    ),
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
        )
