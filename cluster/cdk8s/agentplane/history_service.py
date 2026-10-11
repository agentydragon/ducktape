"""The History Service: takes the Session journals runners push (agentplane/docs/runner_control.md
§ Event ingress), and serves and folds retained history from the `sandbox_service` database, whose
schema its init container migrates.

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
from cdk8s_plus_34 import (
    ContainerResources,
    ContainerSecurityContextProps,
    Cpu,
    CpuResources,
    Deployment,
    ImagePullPolicy,
    MemoryResources,
    PodSecurityContextProps,
    Service,
    ServiceAccount,
)
from constructs import Construct

from agentplane.egress.resources import placeholder_of
from agentplane.history_service.settings import CONFIG_FILE_ENV, RUNNER_INGRESS_AUDIENCE, Settings
from agentplane.sandbox_service.runner_endpoints import RunnerEndpoint
from agentplane.subjects import ServiceAccountRef
from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.agentplane import database, egress, notifications
from cluster.cdk8s.agentplane.environment import Environment
from cluster.cdk8s.agentplane.migrate_container import migrate_init_container
from cluster.cdk8s.agentplane.pod_disruption_budget import add_pod_disruption_budget
from cluster.cdk8s.forgejo_registry.chart import forgejo_images_creds_secret_ref
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
        self, scope: Construct, id: str, env: Environment, *, reader: ServiceAccountRef, caller: ServiceRef
    ) -> None:
        super().__init__(scope, id)
        endpoint = service(env.namespace)
        ingress = ingress_service(env.namespace)
        account = ServiceAccount(
            self, "account", metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace), automount_token=True
        )
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
                "ingress": {
                    "token_audience": RUNNER_INGRESS_AUDIENCE,
                    # Sandboxes run in the environment's own namespace.
                    "writer_namespaces": [env.namespace],
                    "port": ingress.port.number,
                    "settle_deltas": env.settle_deltas,
                },
                "fold_shadow": env.fold_shadow,
            },
            path="/etc/agentplane-history-service/config.yaml",
            supplied=[("database_url",)],
        )
        # The History Service writes the `sandbox_service` database, so it connects as that
        # database's owner; its read API's sessions are read-only (main.py).
        database_url = (
            SecretRef(namespace=env.namespace, name="postgres-sandbox-service").key("uri").env_value(self, "database")
        )
        deployment = Deployment(
            self,
            "deployment",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            pod_metadata=ApiObjectMetadata(labels=_LABELS),
            replicas=env.replicas.count,
            strategy=env.replicas.strategy,
            min_ready=env.replicas.min_ready,
            termination_grace_period=Duration.seconds(30),
            service_account=account,
            automount_service_account_token=True,
            docker_registry_auth=forgejo_images_creds_secret_ref(self, "images-creds"),
            security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
            # A migration failure blocks startup; the command has no retry loop.
            init_containers=[
                migrate_init_container(
                    "git.allegedly.works/ducktape-ci/agentplane-history-migrate:unset",
                    name="migrate",
                    env_variables={"AGENTPLANE_SANDBOX_SERVICE_DATABASE_URL": database_url},
                )
            ],
        )
        container = deployment.add_container(
            name="history-service",
            image=f"{_IMAGE}:unset",
            env_variables={env_name(Settings, "database_url"): database_url},
            image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
            ports=[endpoint.port.container_port(), ingress.port.container_port()],
            readiness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=3, period_seconds=10),
            liveness=grpc_probe(port=endpoint.port.number, initial_delay_seconds=20, period_seconds=30),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(20)),
                memory=MemoryResources(request=Size.mebibytes(128), limit=Size.mebibytes(512)),
            ),
            security_context=ContainerSecurityContextProps(read_only_root_filesystem=False),
        )
        config.mount_into(container, env=CONFIG_FILE_ENV)
        pod_policy.place(deployment, node_scheduling.HIL_OVH)
        pod_policy.harden(deployment)
        Service(
            self,
            "service",
            metadata=ApiObjectMetadata(name=NAME, namespace=env.namespace, labels=_LABELS),
            selector=deployment,
            ports=[endpoint.port.service_port(), ingress.port.service_port()],
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
                EgressRule.to_endpoints(
                    {"k8s:io.kubernetes.pod.namespace": env.namespace, "k8s:cnpg.io/cluster": "postgres"},
                    database.POSTGRES_PORT,
                ),
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
        if env.replicas.pdb_min_available is not None:
            add_pod_disruption_budget(
                self,
                "pdb",
                name=NAME,
                namespace=env.namespace,
                min_available=env.replicas.pdb_min_available,
                selector=_LABELS,
            )
