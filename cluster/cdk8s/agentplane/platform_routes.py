"""The Agentplane services runners call over the egress sidecar's platform routes
(agentplane/egress/sidecar/configpb/config.proto). From this one list come:
- the sidecar's routes and the runner's endpoint flags in the runner SandboxTemplates;
- the network rules on both ends.

Authenticated routes use the token the sidecar projects for their audience (sandbox_pod.py). The
telemetry route is an unauthenticated local hop to Alloy. Neither route uses the central proxy.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import ApiObjectMetadata
from constructs import Construct

from agentplane.egress.sidecar.configpb import config_pb2
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from cluster.cdk8s.agentplane import history_service, sandbox_pod, telemetry
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef


@dataclass(frozen=True)
class Route:
    name: str
    service: ServiceRef
    audience: str | None
    # On the sidecar's loopback, beside its proxy (3128) and readiness (3129) ports.
    listen_port: int

    @property
    def local_target(self) -> str:
        return f"127.0.0.1:{self.listen_port}"


# Each route replaces `authorization` with its own token, so a runner presents this placeholder.
ROUTE_BEARER = "platform-route"


def ingress(namespace: str) -> Route:
    return Route("ingress", history_service.ingress_service(namespace), RUNNER_INGRESS_AUDIENCE, 3131)


def telemetry_route() -> Route:
    """The runner's local, unauthenticated gRPC hop to Alloy; the runner never dials Alloy directly."""
    return Route("telemetry", telemetry.ALLOY_OTLP_GRPC, None, 3132)


def routes(namespace: str) -> tuple[Route, ...]:
    return ingress(namespace), telemetry_route()


def sidecar_routes(namespace: str, *, listen_host: str = "127.0.0.1") -> dict[str, config_pb2.PlatformRoute]:
    """The sidecar's `platform_routes` setting."""
    return {
        route.name: config_pb2.PlatformRoute(
            listen_address=f"{listen_host}:{route.listen_port}",
            upstream=f"{route.service.fqdn}:{route.service.pod_port}",
            token_file=sandbox_pod.audience_token_file(route.audience) if route.audience else "",
        )
        for route in routes(namespace)
    }


def telemetry_endpoint(*, host: str = "127.0.0.1") -> str:
    """The OTLP/gRPC endpoint on the sidecar route, in the caller's network namespace."""
    return f"http://{host}:{telemetry_route().listen_port}"


class PlatformRouteFences(Construct):
    """`clients` may reach each route's service directly: an egress rule on them and an ingress rule
    on the service. Cilium unions these with each side's own policy."""

    def __init__(self, scope: Construct, id: str, *, namespace: str, clients: Pods) -> None:
        super().__init__(scope, id)
        NetworkPolicy(
            self,
            "clients",
            metadata=ApiObjectMetadata(name="platform-route-clients", namespace=namespace),
            endpoint_selector=clients.selector,
            egress=[route.service.egress() for route in routes(namespace)],
        )
        for route in routes(namespace):
            service_namespace = route.service.pods.namespace
            policy_name = (
                f"platform-route-{route.name}"
                if service_namespace == namespace
                else f"platform-route-{namespace}-{route.name}"
            )
            NetworkPolicy(
                self,
                route.name,
                metadata=ApiObjectMetadata(name=policy_name, namespace=service_namespace),
                endpoint_selector=route.service.pods.selector,
                ingress=[clients.admit(route.service.pod_port)],
            )
