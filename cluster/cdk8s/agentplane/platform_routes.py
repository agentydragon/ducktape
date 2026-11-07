"""The Agentplane services runners call over the egress sidecar's platform routes
(agentplane/egress/platform_routes.py). From this one list come:
- the sidecar's routes and the runner's endpoint flags in the runner SandboxTemplates;
- the network rules on both ends.

Authenticated routes use the token the sidecar projects for their audience (sandbox_pod.py). The
telemetry route is an unauthenticated local hop to Alloy. Neither route uses the central proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObjectMetadata
from constructs import Construct

from agentplane.egress.platform_routes import PLATFORM_ROUTES_PORT, PlatformRoute, PlatformRoutes
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from agentplane.workload_auth.audience import WORKLOAD_TOKEN_AUDIENCE
from cluster.cdk8s.agentplane import egress, history_service, llm_ingress, notifications, sandbox_pod, telemetry
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef


@dataclass(frozen=True)
class Route:
    name: str
    service: ServiceRef
    audience: str | None
    service_prefixes: tuple[str, ...]

    @property
    def listen_port(self) -> int:
        # All gRPC services share one local listener; full methods select the upstream route.
        return PLATFORM_ROUTES_PORT

    @property
    def local_target(self) -> str:
        return f"127.0.0.1:{self.listen_port}"


# Each route replaces `authorization` with its own token, so a runner presents this placeholder.
ROUTE_BEARER = "platform-route"


def ingress(namespace: str) -> Route:
    return Route(
        "ingress",
        history_service.ingress_service(namespace),
        RUNNER_INGRESS_AUDIENCE,
        ("/agentplane.history_service.SessionJournalIngress/",),
    )


def egress_rules(namespace: str) -> Route:
    return Route("egress-rules", egress.admin(namespace), WORKLOAD_TOKEN_AUDIENCE, ("/agentplane.egress.EgressRules/",))


def model_config(namespace: str) -> Route:
    return Route(
        "model-config",
        llm_ingress.grpc_service(namespace),
        WORKLOAD_TOKEN_AUDIENCE,
        ("/agentplane.llm_ingress.v1.ModelConfigService/",),
    )


def notifications_grpc(namespace: str) -> Route:
    return Route(
        "notifications",
        notifications.service(namespace),
        notifications.TOKEN_AUDIENCE,
        ("/agentplane.notification_service.Notifications/",),
    )


def telemetry_route() -> Route:
    """The runner's local, unauthenticated gRPC hop to Alloy; the runner never dials Alloy directly."""
    return Route(
        "telemetry",
        telemetry.ALLOY_OTLP_GRPC,
        None,
        (
            "/opentelemetry.proto.collector.trace.v1.TraceService/",
            "/opentelemetry.proto.collector.metrics.v1.MetricsService/",
            "/opentelemetry.proto.collector.logs.v1.LogsService/",
        ),
    )


def routes(namespace: str) -> tuple[Route, ...]:
    return (
        ingress(namespace),
        telemetry_route(),
        egress_rules(namespace),
        notifications_grpc(namespace),
        model_config(namespace),
    )


def sidecar_routes(namespace: str, *, listen_host: str = "127.0.0.1") -> PlatformRoutes:
    """The sidecar's one listener and independently authenticated upstream routes."""
    return PlatformRoutes(
        listen_address=f"{listen_host}:{PLATFORM_ROUTES_PORT}",
        routes={
            route.name: PlatformRoute(
                service_prefixes=route.service_prefixes,
                upstream=f"{route.service.fqdn}:{route.service.pod_port}",
                **({"token_file": Path(sandbox_pod.audience_token_file(route.audience))} if route.audience else {}),
            )
            for route in routes(namespace)
        },
    )


def telemetry_endpoint(*, host: str = "127.0.0.1") -> str:
    """The OTLP/gRPC endpoint on the shared sidecar listener."""
    return f"http://{host}:{PLATFORM_ROUTES_PORT}"


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
