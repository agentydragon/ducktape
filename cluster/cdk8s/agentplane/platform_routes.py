"""The Agentplane services runners call over the egress sidecar's shared platform listener
(agentplane/egress/platform_routes.py). From this one list come:
- the sidecar's routes and the runner's endpoint flags in the runner SandboxTemplates;
- the network rules on both ends.

Each route keeps its own upstream and transport. Plaintext routes use a sidecar-projected bearer
token when the service requires one; telemetry is unauthenticated. In runner SVID mode, only Egress
Rules uses TLS through the central proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObjectMetadata
from constructs import Construct

from agentplane.egress.platform_routes import (
    PLATFORM_ROUTES_PORT,
    PlaintextRouteTransport,
    PlatformRoute,
    PlatformRoutes,
    PlatformRouteTransport,
    TLSConnectRouteTransport,
)
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import egress, history_service, llm_ingress, notifications, sandbox_pod, telemetry
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef

# The egress proxy's SPIFFE server identity, also used by its issued Certificate.
EGRESS_PROXY_URI = "spiffe://agentplane/egress-proxy"


@dataclass(frozen=True)
class Route:
    name: str
    service: ServiceRef
    transport: PlatformRouteTransport
    service_prefixes: tuple[str, ...]

    @property
    def listen_port(self) -> int:
        # All gRPC services share one local listener; full methods select the upstream route.
        return PLATFORM_ROUTES_PORT

    @property
    def local_target(self) -> str:
        return f"127.0.0.1:{self.listen_port}"


# Bearer-authenticated routes replace `authorization` with their projected token.
ROUTE_BEARER = "platform-route"


def ingress(namespace: str) -> Route:
    return Route(
        "ingress",
        history_service.ingress_service(namespace),
        PlaintextRouteTransport(token_file=Path(sandbox_pod.audience_token_file(RUNNER_INGRESS_AUDIENCE))),
        ("/agentplane.history_service.SessionJournalIngress/",),
    )


def egress_rules(namespace: str, *, runner_egress_mtls: bool = False) -> Route:
    transport: PlatformRouteTransport
    if runner_egress_mtls:
        transport = TLSConnectRouteTransport(server_uri=EGRESS_PROXY_URI)
    else:
        transport = PlaintextRouteTransport(token_file=Path(sandbox_pod.audience_token_file(WORKLOAD_TOKEN_AUDIENCE)))
    return Route("egress-rules", egress.admin(namespace), transport, ("/agentplane.egress.EgressRules/",))


def model_config(namespace: str) -> Route:
    return Route(
        "model-config",
        llm_ingress.grpc_service(namespace),
        PlaintextRouteTransport(token_file=Path(sandbox_pod.audience_token_file(WORKLOAD_TOKEN_AUDIENCE))),
        ("/agentplane.llm_ingress.v1.ModelConfigService/",),
    )


def notifications_grpc(namespace: str) -> Route:
    return Route(
        "notifications",
        notifications.service(namespace),
        PlaintextRouteTransport(token_file=Path(sandbox_pod.audience_token_file(notifications.TOKEN_AUDIENCE))),
        ("/agentplane.notification_service.Notifications/",),
    )


def telemetry_route() -> Route:
    """The runner's local, unauthenticated gRPC hop to Alloy; the runner never dials Alloy directly."""
    return Route(
        "telemetry",
        telemetry.ALLOY_OTLP_GRPC,
        PlaintextRouteTransport(),
        (
            "/opentelemetry.proto.collector.trace.v1.TraceService/",
            "/opentelemetry.proto.collector.metrics.v1.MetricsService/",
            "/opentelemetry.proto.collector.logs.v1.LogsService/",
        ),
    )


def routes(namespace: str, *, runner_egress_mtls: bool = False) -> tuple[Route, ...]:
    return (
        ingress(namespace),
        telemetry_route(),
        egress_rules(namespace, runner_egress_mtls=runner_egress_mtls),
        notifications_grpc(namespace),
        model_config(namespace),
    )


def sidecar_routes(
    namespace: str, *, listen_host: str = "127.0.0.1", runner_egress_mtls: bool = False
) -> PlatformRoutes:
    """The sidecar's one listener and independently authenticated upstream routes."""
    return PlatformRoutes(
        listen_address=f"{listen_host}:{PLATFORM_ROUTES_PORT}",
        routes={
            route.name: PlatformRoute(
                service_prefixes=route.service_prefixes,
                upstream=f"{route.service.fqdn}:{route.service.pod_port}",
                transport=route.transport,
            )
            for route in routes(namespace, runner_egress_mtls=runner_egress_mtls)
        },
    )


def telemetry_endpoint(*, host: str = "127.0.0.1") -> str:
    """The OTLP/gRPC endpoint on the shared sidecar listener."""
    return f"http://{host}:{PLATFORM_ROUTES_PORT}"


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
