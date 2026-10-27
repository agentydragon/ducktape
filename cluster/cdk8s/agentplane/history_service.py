"""The History Service: takes the Session journals runners push (agentplane/docs/runner_control.md
§ Event ingress), and serves and folds retained history from the `sandbox_service` database, whose
schema its init container migrates.

Runners reach the ingress listener only through the egress proxy, which swaps the placeholder
bearer for the Pod's own token for the ingress audience. It takes untrusted input, so it listens
apart from the read API, and the network fence admits the egress proxy to it alone.
"""

from collections.abc import Mapping

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    EnvValue,
    ImagePullPolicy,
    MemoryResources,
    Role,
    RoleBinding,
    RolePolicyRule,
    ServiceAccount,
)
from constructs import Construct

from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling
from cluster.cdk8s.agentplane import database, egress
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.api_resource import custom_resource
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name, validate_settings_fragment

NAME = "history-service"
TOKEN_AUDIENCE = "agentplane-history-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-service"
# The policy a runner behind the egress proxy reaches the ingress under (the KubeVirt experiment's;
# a Sandbox's runner uses its sidecar's platform route), and the credential it substitutes.
EGRESS_POLICY = "runner-ingress"
_CREDENTIAL = "agentplane-runner-ingress"
_PUSH = "/agentplane.history_service.RunnerIngress/Push"


_DEFAULT_MEMORY_LIMIT = Size.mebibytes(512)
_DEFAULT_MIGRATE_MEMORY_LIMIT = Size.mebibytes(256)


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def ingress_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="runner-ingress", number=8081),
        pods=Pods(namespace=namespace, labels=tuple(_LABELS.items())),
    )


class HistoryService(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        database_url: EnvValue,
        callers: dict[ServiceAccountRef, Pods],
        placement: node_scheduling.Placement | None = node_scheduling.HIL_OVH,
        registry_secret_name: str | None = "forgejo-images-creds",
        container_env: Mapping[str, EnvValue] | None = None,
        migrate_env: Mapping[str, EnvValue] | None = None,
        memory_limit: Size | None = _DEFAULT_MEMORY_LIMIT,
        migrate_pull_policy: ImagePullPolicy = ImagePullPolicy.ALWAYS,
        migrate_memory_limit: Size | None = _DEFAULT_MIGRATE_MEMORY_LIMIT,
    ) -> None:
        super().__init__(scope, id)
        self.env = env
        self.callers = callers
        endpoint = service(env.namespace)
        ingress = ingress_service(env.namespace)
        # TokenReview proves readers' and runners' projected workload tokens.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-history-service-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        Role(
            self,
            "service-account-reader-role",
            metadata=ApiObjectMetadata(name=f"{NAME}-service-accounts", namespace=env.namespace),
            rules=[RolePolicyRule(resources=[custom_resource("", "serviceaccounts")], verbs=["get"])],
        )
        RoleBinding(
            self,
            "service-account-reader-rolebinding",
            metadata=ApiObjectMetadata(name=f"{NAME}-service-accounts", namespace=env.namespace),
            role=Role.from_role_name(self, "service-account-reader-role-ref", f"{NAME}-service-accounts"),
        ).add_subjects(
            ServiceAccount.from_service_account_name(
                self, "history-service-account", NAME, namespace_name=env.namespace
            )
        )
        content = {
            "reader_accounts": [
                reader.model_dump() for reader in sorted(callers, key=lambda reader: (reader.namespace, reader.name))
            ],
            "token_audience": TOKEN_AUDIENCE,
            "listen_address": f"0.0.0.0:{endpoint.pod_port}",
            "ingress": {
                "token_audience": RUNNER_INGRESS_AUDIENCE,
                # Sandboxes run in the environment's own namespace.
                "writer_namespaces": [env.namespace],
                "listen_address": f"0.0.0.0:{ingress.pod_port}",
                "settle_deltas": env.settle_deltas,
            },
        }
        validate_settings_fragment(Settings, content)
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            content=yaml_config(content),
            path="/etc/agentplane-history-service/config.yaml",
        )
        # The History Service writes the `sandbox_service` database, so it connects as that
        # database's owner; its read API's sessions are read-only (main.go).
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint, ingress],
            container="history-service",
            image=f"{_IMAGE}:unset",
            env_variables={env_name(Settings, "database_url"): database_url, **(container_env or {})},
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=memory_limit),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(
                "git.allegedly.works/ducktape-ci/agentplane-history-migrate:unset",
                name="migrate",
                env_variables={"AGENTPLANE_HISTORY_SERVICE_DATABASE_URL": database_url, **(migrate_env or {})},
                image_pull_policy=migrate_pull_policy,
                memory_limit=migrate_memory_limit,
            ),
            placement=placement,
            telemetry_enabled=True,
            registry_secret_name=registry_secret_name,
        )
        config.mount_into(workload.container, env=Settings.config_file_env)
        egress.add_runner_egress(
            self,
            policy=EGRESS_POLICY,
            credential=_CREDENTIAL,
            endpoint=ingress,
            path=_PUSH,
            audience=RUNNER_INGRESS_AUDIENCE,
            purpose="pushes to the History Service ingress",
        )

    def add_platform_resources(self) -> None:
        """Add fleet network admission."""
        env = self.env
        endpoint = service(env.namespace)
        ingress = ingress_service(env.namespace)
        proxy = egress.proxy(env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME.removeprefix("agentplane-"), namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                *(pods.admit(endpoint.pod_port) for pods in self.callers.values()),
                proxy.pods.admit(ingress.pod_port),
            ],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER), database.egress(env.namespace)],
        )
        egress.add_runner_egress_network_policy(self, policy=EGRESS_POLICY, endpoint=ingress)
