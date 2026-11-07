"""The Agentplane services runners call over the egress sidecar's platform routes
(agentplane/egress/platform_routes.py). From this one list come:
- the sidecar's routes and the runner's endpoint flags in the runner SandboxTemplates;
- the network rules on both ends.

The sidecar presents its SVID to Rules, Notification and model configuration through the central
proxy. Telemetry remains a direct unauthenticated route.
"""

from __future__ import annotations

from dataclasses import dataclass

from cdk8s import ApiObjectMetadata
from constructs import Construct

from agentplane.egress.platform_routes import (
    PlaintextRouteTransport,
    PlatformRoute,
    PlatformRouteTransport,
    TLSConnectRouteTransport,
)
from cluster.cdk8s.agentplane import egress, llm_ingress, notifications, telemetry
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef


@dataclass(frozen=True)
class Route:
    name: str
    service: ServiceRef
    transport: PlatformRouteTransport
    # On the sidecar's loopback, beside its proxy (3128) and readiness (3129) ports.
    listen_port: int

    @property
    def local_target(self) -> str:
        return f"127.0.0.1:{self.listen_port}"


# Runtime's local model-config client sends this inert placeholder; the TLS route discards it.
ROUTE_BEARER = "platform-route"


def egress_rules(namespace: str) -> Route:
    return Route(
        "egress-rules",
        egress.admin(namespace),
        TLSConnectRouteTransport(server_uri="spiffe://agentplane/egress-proxy"),
        3133,
    )


def model_config(namespace: str) -> Route:
    return Route(
        "model-config",
        llm_ingress.workload_service(namespace),
        TLSConnectRouteTransport(server_uri=llm_ingress.WORKLOAD_URI),
        3135,
    )


def notifications_grpc(namespace: str) -> Route:
    return Route(
        "notifications",
        notifications.workload_service(namespace),
        TLSConnectRouteTransport(server_uri=notifications.WORKLOAD_URI),
        3134,
    )


def telemetry_route() -> Route:
    """The local, unauthenticated gRPC hop to Alloy."""
    return Route("telemetry", telemetry.ALLOY_OTLP_GRPC, PlaintextRouteTransport(), 3132)


def routes(namespace: str) -> tuple[Route, ...]:
    return telemetry_route(), egress_rules(namespace), notifications_grpc(namespace), model_config(namespace)


def sidecar_routes(namespace: str, *, listen_host: str = "127.0.0.1") -> dict[str, PlatformRoute]:
    """The sidecar's `platform_routes` setting."""
    return {
        route.name: PlatformRoute(
            listen_address=f"{listen_host}:{route.listen_port}",
            upstream=f"{route.service.fqdn}:{route.service.pod_port}",
            transport=route.transport,
        )
        for route in routes(namespace)
    }


def telemetry_endpoint(*, host: str = "127.0.0.1") -> str:
    """The OTLP/gRPC endpoint on the sidecar route, in the caller's network namespace."""
    return f"http://{host}:{telemetry_route().listen_port}"


class PlatformRouteFences(Construct):
    """`clients` may reach each route's service directly: egress on them and ingress on the service.
    Cilium unions these with each side's own policy."""

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
