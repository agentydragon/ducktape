"""The static browser shell, routed beside the app API on the same public origin."""

from cdk8s import ApiObjectMetadata, Duration, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    DeploymentStrategy,
    MemoryResources,
    PercentOrAbsolute,
    Volume,
)
from constructs import Construct

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.agentplane.environment import Environment, ReplicaProfile
from cluster.cdk8s.agentplane.service_workload import add_service_workload
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.probes import http_probe
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy, deny_all_egress
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "frontend"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-frontend"
_REPLICAS = ReplicaProfile(
    count=2,
    strategy=DeploymentStrategy.rolling_update(
        max_surge=PercentOrAbsolute.absolute(1), max_unavailable=PercentOrAbsolute.absolute(0)
    ),
    min_ready=Duration.seconds(5),
    pdb_min_available=1,
)


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="http", number=8080),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )


class Frontend(Construct):
    """Static files without API credentials, Kubernetes access or outbound connections."""

    def __init__(self, scope: Construct, id: str, env: Environment) -> None:
        super().__init__(scope, id)
        self.env = env
        self.service = service(env.namespace)
        workload = add_service_workload(
            self,
            env,
            serves=[self.service],
            container=NAME,
            image=f"{_IMAGE}:unset",  # image-pins/ supplies the independently published frontend tag.
            readiness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=1),
            liveness=http_probe("/healthz", port=self.service.pod_port, initial_delay_seconds=5),
            resources=ContainerResources(
                cpu=CpuResources(request=Cpu.millis(10)),
                memory=MemoryResources(request=Size.mebibytes(32), limit=Size.mebibytes(128)),
            ),
            termination_grace=Duration.seconds(30),
            replica_profile=_REPLICAS,
            placement=node_scheduling.HIL_OVH,
            registry_secret_name="forgejo-images-creds",
            automount_service_account_token=False,
            read_only_root_filesystem=True,
        )
        workload.container.mount("/tmp", Volume.from_empty_dir(self, "tmp", "tmp"))

    def add_platform_resources(self) -> None:
        # Exact root matches preserve existing root-level API URLs. The UI uses /#/ routes.
        https_route(
            self,
            "httproute",
            metadata=ApiObjectMetadata(name=NAME, namespace=self.env.namespace),
            hostnames=[self.env.app.hostname],
            backend=self.service,
            paths=["/", "/index.html", "/main.js", "/main.css", "/sw.js", "/favicon.svg"],
        )
        NetworkPolicy(
            self,
            "networkpolicy",
            metadata=ApiObjectMetadata(name=NAME, namespace=self.env.namespace),
            endpoint_selector=self.service.pods.selector,
            ingress=[IngressRule.from_gateway(self.service.pod_port)],
            egress_deny=deny_all_egress(),
        )
