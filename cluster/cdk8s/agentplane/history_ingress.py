"""The History Service's runner ingress (agentplane/docs/runner_control.md § Event ingress): runners
push their Session journals here through the egress proxy, which swaps the placeholder bearer for
the Pod's own token for this audience. It takes untrusted input, so it runs apart from the read API.
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
from agentplane.history_service.ingress_settings import CONFIG_FILE_ENV, RUNNER_INGRESS_AUDIENCE, IngressSettings
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from cluster.cdk8s import cilium
from cluster.cdk8s.agentplane import database, egress
from cluster.cdk8s.agentplane.environment import Environment
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

NAME = "agentplane-history-ingress"
# The policy a Sandbox's runner reaches the ingress under, and the credential it substitutes.
EGRESS_POLICY = "runner-ingress"
_CREDENTIAL = "agentplane-runner-ingress"
# The bearer a runner sends on its pushes.
BEARER = placeholder_of(_CREDENTIAL)
_PUSH = "/agentplane.proto.runner_ingress.RunnerIngress/Push"
_LABELS = {"app.kubernetes.io/name": NAME}
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-history-ingress"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def runner_ingress_endpoint(namespace: str) -> RunnerEndpoint:
    """The endpoint every new Sandbox's runner pushes its Events to: through the egress proxy, which
    swaps the placeholder bearer for the Pod's token."""
    endpoint = service(namespace)
    return RunnerEndpoint(target=f"{endpoint.fqdn}:{endpoint.port.number}", bearer=BEARER, egress_policy=EGRESS_POLICY)


class HistoryIngress(Construct):
    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
        # TokenReview proves each runner's projected workload token.
        token_reviewer_cluster_rbac(
            self,
            "token-reviewer",
            name=f"{env.namespace}-history-ingress-token-reviewer",
            service_account_name=NAME,
            namespace=env.namespace,
        )
        config = SettingsFile(
            self,
            "config",
            metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=env.namespace),
            model=IngressSettings,
            content={
                "token_audience": RUNNER_INGRESS_AUDIENCE,
                # Sandboxes run in the environment's own namespace.
                "writer_namespaces": [env.namespace],
                "port": endpoint.port.number,
                "settle_deltas": env.settle_deltas,
            },
            path="/etc/agentplane-history-ingress/config.yaml",
            supplied=[("database_url",)],
        )
        workload = add_service_workload(
            self,
            env,
            serves=[endpoint],
            container="history-ingress",
            image=f"{_IMAGE}:unset",
            # Appends to the history the History Service serves, so it connects as the same owner.
            env_variables={
                env_name(IngressSettings, "database_url"): SecretRef(
                    namespace=env.namespace, name="postgres-sandbox-service"
                )
                .key("uri")
                .env_value(self, "database")
            },
            ports=[endpoint.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            termination_grace=Duration.seconds(30),
        )
        config.mount_into(workload.container, env=CONFIG_FILE_ENV)
        proxy = egress.proxy(env.namespace)
        NetworkPolicy(
            self,
            "network-policy",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace),
            endpoint_selector=endpoint.pods.selector,
            ingress=[proxy.pods.admit(endpoint.pod_port)],
            egress=[cilium.dns_egress(), EgressRule.to_entities(Entity.KUBE_APISERVER), database.egress(env.namespace)],
        )
        EgressCredential(
            self,
            "credential",
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
            "policy",
            metadata=ApiObjectMetadata(name=EGRESS_POLICY, namespace=env.namespace),
            rules=[
                EgressPolicySpecRules(
                    hosts=[endpoint.fqdn],
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
            "proxy-egress",
            metadata=ApiObjectMetadata(name=f"{NAME}-proxy", namespace=env.namespace),
            endpoint_selector=proxy.pods.selector,
            egress=[endpoint.egress()],
        )
