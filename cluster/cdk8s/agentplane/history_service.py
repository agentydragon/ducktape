"""The History Service: takes the Session journals runners push (agentplane/docs/runner_control.md
§ Event ingress), and serves and folds retained history from the `sandbox_service` database, whose
schema its init container migrates.

Runners reach the ingress listener only through the egress proxy, which swaps the placeholder
bearer for the Pod's own token for the ingress audience. It takes untrusted input, so it listens
apart from the read API, and the network fence admits the egress proxy to it alone.
"""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources
from constructs import Construct

from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE, Settings
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database, egress
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, Entity, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.cdk8s.settings_file import SettingsFile
from cluster.cdk8s.token_reviewer_rbac import token_reviewer_cluster_rbac
from util.settings_contract import env_name

NAME = "agentplane-history-service"
TOKEN_AUDIENCE = "agentplane-history-service"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-service"
# The policy a runner behind the egress proxy reaches the ingress under (the KubeVirt experiment's;
# a Sandbox's runner uses its sidecar's platform route), and the credential it substitutes.
EGRESS_POLICY = "runner-ingress"
_CREDENTIAL = "agentplane-runner-ingress"
_PUSH = "/agentplane.proto.runner_ingress.RunnerIngress/Push"


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
    def __init__(self, scope: Construct, id: str, env: Environment, *, callers: dict[ServiceAccountRef, Pods]) -> None:
        super().__init__(scope, id)
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
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=Settings,
            content={
                "reader_accounts": [
                    reader.model_dump()
                    for reader in sorted(callers, key=lambda reader: (reader.namespace, reader.name))
                ],
                "token_audience": TOKEN_AUDIENCE,
                "port": endpoint.port.number,
                "ingress": {
                    "token_audience": RUNNER_INGRESS_AUDIENCE,
                    # Sandboxes run in the environment's own namespace.
                    "writer_namespaces": [env.namespace],
                    "port": ingress.port.number,
                    "settle_deltas": env.settle_deltas,
                },
            },
            path="/etc/agentplane-history-service/config.yaml",
            supplied=[("database_url",)],
        )
        # The History Service writes the `sandbox_service` database, so it connects as that
        # database's owner; its read API's sessions are read-only (main.py).
        database_url = (
            SecretRef(namespace=env.namespace, name="postgres-sandbox-service").key("uri").env_value(self, "database")
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint, ingress],
            container="history-service",
            image=f"{_IMAGE}:unset",
            env_variables={env_name(Settings, "database_url"): database_url},
            ports=[endpoint.port.container_port(), ingress.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
            migrate=migrate_init_container(
                "git.allegedly.works/ducktape-ci/agentplane-history-migrate:unset",
                name="migrate",
                env_variables={"AGENTPLANE_HISTORY_SERVICE_DATABASE_URL": database_url},
            ),
        )
        config.mount_into(workload.container, env=Settings.config_file_env)
        proxy = egress.proxy(env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[*(pods.admit(endpoint.pod_port) for pods in callers.values()), proxy.pods.admit(ingress.pod_port)],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER), database.egress(env.namespace)],
        )
        egress.add_runner_egress(
            self,
            policy=EGRESS_POLICY,
            credential=_CREDENTIAL,
            endpoint=ingress,
            path=_PUSH,
            audience=RUNNER_INGRESS_AUDIENCE,
            purpose="pushes to the History Service ingress",
        )
