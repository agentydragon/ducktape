"""The History Service: takes the Session journals runners push (agentplane/docs/runner_control.md
§ Event ingress), follows the rest through the Runner Gateway, and serves retained history from the
`sandbox_service` database, whose schema its init container migrates.

Runners reach the ingress listener only through the egress proxy, which swaps the placeholder
bearer for the Pod's own token for the ingress audience. It takes untrusted input, so it listens
apart from the read API, and the network fence admits Sandboxes to it alone.
"""

from agentplane_egresscredential_crds.works.allegedly.agentplane import (
    EgressCredentialSpecTargets,
    EgressCredentialSpecTargetsMethod,
)
from agentplane_egresspolicy_crds.works.allegedly.agentplane import (
    EgressPolicySpecRules,
    EgressPolicySpecRulesCredentialRef,
    EgressPolicySpecRulesMethods,
)
from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import ContainerResources, Cpu, CpuResources, MemoryResources
from constructs import Construct

from agentplane.egress.resources import placeholder_of
from agentplane.history_service.settings import CONFIG_FILE_ENV, RUNNER_INGRESS_AUDIENCE, Settings
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database, egress, notifications
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.probes import grpc_probe
from cluster.cdk8s.providers.agentplane.egress_credential import EgressCredential, Source
from cluster.cdk8s.providers.agentplane.egress_policy import EgressPolicy
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
_SERVICE_TOKEN_DIR = "/var/run/secrets/history-service"
# The policy a Sandbox's runner reaches the ingress under, and the credential it substitutes.
EGRESS_POLICY = "runner-ingress"
_CREDENTIAL = "agentplane-runner-ingress"
# The bearer a runner sends on its pushes.
BEARER = placeholder_of(_CREDENTIAL)
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


def runner_ingress_endpoint(namespace: str) -> RunnerEndpoint:
    """The endpoint every new Sandbox's runner pushes its Events to: through the egress proxy, which
    swaps the placeholder bearer for the Pod's token."""
    endpoint = ingress_service(namespace)
    return RunnerEndpoint(target=f"{endpoint.fqdn}:{endpoint.port.number}", bearer=BEARER, egress_policy=EGRESS_POLICY)


class HistoryService(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        env: Environment,
        *,
        reader: ServiceAccountRef,
        caller: ServiceRef,
        sandboxes: ServiceRef,
        sandboxes_audience: str,
        gateway: ServiceRef,
        gateway_audience: str,
    ) -> None:
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
                # The app reads Threads; the Notification Service reads command receipts.
                "reader_accounts": [
                    reader.model_dump(),
                    ServiceAccountRef(namespace=env.namespace, name=notifications.NAME).model_dump(),
                ],
                "token_audience": TOKEN_AUDIENCE,
                "port": endpoint.port.number,
                "sandbox_service": {
                    "target": f"{sandboxes.fqdn}:{sandboxes.pod_port}",
                    "gateway_target": f"{gateway.fqdn}:{gateway.pod_port}",
                    "token_file": f"{_SERVICE_TOKEN_DIR}/sandboxes",
                    "gateway_token_file": f"{_SERVICE_TOKEN_DIR}/gateway",
                    "grpc_channel_options": env.app_config.sandbox_service_grpc_channel_options,
                },
                "ingester": {"settle_deltas": env.settle_deltas},
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
                env_variables={"AGENTPLANE_SANDBOX_SERVICE_DATABASE_URL": database_url},
            ),
        )
        config.mount_into(workload.container, env=CONFIG_FILE_ENV)
        # For the ingester's Sandbox Service and Runner Gateway calls.
        workload.project_tokens(
            "service-tokens", _SERVICE_TOKEN_DIR, {"sandboxes": sandboxes_audience, "gateway": gateway_audience}
        )
        proxy = egress.proxy(env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[
                caller.pods.admit(endpoint.pod_port),
                notifications.service(env.namespace).pods.admit(endpoint.pod_port),
                proxy.pods.admit(ingress.pod_port),
            ],
            egress=[
                cilium.dns_egress(),
                EgressRule.to_entities(Entity.KUBE_APISERVER),
                sandboxes.egress(),
                gateway.egress(),
                database.egress(env.namespace),
            ],
        )
        EgressCredential(
            self,
            "runner-ingress-credential",
            metadata=ApiObjectMetadata(name=_CREDENTIAL, namespace=env.namespace),
            description=(
                "The calling Sandbox Pod's short-lived, Pod-bound identity minted only for its runner's "
                "pushes to the History Service ingress. It conveys no other authority."
            ),
            source=Source.projected_workload_token(audience=RUNNER_INGRESS_AUDIENCE),
            targets=[
                EgressCredentialSpecTargets(
                    header="Authorization", method=EgressCredentialSpecTargetsMethod.SCHEME_TOKEN, scheme="Bearer"
                )
            ],
        )
        EgressPolicy(
            self,
            "runner-ingress-policy",
            metadata=ApiObjectMetadata(name=EGRESS_POLICY, namespace=env.namespace),
            rules=[
                EgressPolicySpecRules(
                    hosts=[ingress.fqdn],
                    cluster_internal=True,
                    methods=[EgressPolicySpecRulesMethods.POST],
                    paths=[_PUSH],
                    credential_ref=EgressPolicySpecRulesCredentialRef(name=_CREDENTIAL),
                )
            ],
        )
        # Additive to the proxy's own policy, which egress.py owns and which cannot name this
        # module: this one imports that one.
        NetworkPolicy(
            self,
            "runner-ingress-proxy-egress",
            metadata=ApiObjectMetadata(name=f"{NAME}-runner-ingress-proxy", namespace=env.namespace),
            endpoint_selector=proxy.pods.selector,
            egress=[ingress.egress()],
        )
