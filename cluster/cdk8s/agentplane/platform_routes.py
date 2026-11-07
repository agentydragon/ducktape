"""The Agentplane services runners call over the egress sidecar's platform routes
(agentplane/egress/platform_routes.py). From this one list come:
- the sidecar's routes and the runner's endpoint flags in the runner SandboxTemplates;
- the network rules on both ends.

Plaintext routes replace `authorization` with a sidecar-projected audience token when they have one;
telemetry is unauthenticated. In runner SVID mode, only Egress Rules uses TLS through the central
proxy. Network fences mirror the direct routes for each environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cdk8s import ApiObjectMetadata
from constructs import Construct

from agentplane.egress.platform_routes import PlaintextRouteTransport, PlatformRoute, TLSConnectRouteTransport
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
    transport: Literal["plaintext", "tls_connect"]
    # On the sidecar's loopback, beside its proxy (3128) and readiness (3129) ports.
    listen_port: int

    @property
    def local_target(self) -> str:
        return f"127.0.0.1:{self.listen_port}"


# Bearer-authenticated routes replace `authorization` with their projected token.
ROUTE_BEARER = "platform-route"


def ingress(namespace: str) -> Route:
    return Route("ingress", history_service.ingress_service(namespace), RUNNER_INGRESS_AUDIENCE, "plaintext", 3131)


def egress_rules(namespace: str) -> Route:
    return Route(
        "egress-rules",
        egress.admin(namespace),
        None,
        "tls_connect",
        3133,
    )


def model_config(namespace: str) -> Route:
    return Route("model-config", llm_ingress.grpc_service(namespace), WORKLOAD_TOKEN_AUDIENCE, "plaintext", 3135)


def notifications_grpc(namespace: str) -> Route:
    return Route("notifications", notifications.service(namespace), notifications.TOKEN_AUDIENCE, "plaintext", 3134)


def telemetry_route() -> Route:
    """The runner's local, unauthenticated gRPC hop to Alloy; the runner never dials Alloy directly."""
    return Route("telemetry", telemetry.ALLOY_OTLP_GRPC, None, "plaintext", 3132)


def routes(namespace: str) -> tuple[Route, ...]:
    return telemetry_route(), egress_rules(namespace), notifications_grpc(namespace), model_config(namespace)


def sidecar_routes(
    namespace: str, *, listen_host: str = "127.0.0.1"
) -> dict[str, PlatformRoute]:
    """The sidecar's `platform_routes` setting."""
    return {
        route.name: PlatformRoute(
            listen_address=f"{listen_host}:{route.listen_port}",
            upstream=f"{route.service.fqdn}:{route.service.pod_port}",
            transport=(
                TLSConnectRouteTransport()
                if route.transport == "tls_connect"
                else PlaintextRouteTransport(
                    token_file=Path(sandbox_pod.audience_token_file(route.audience)) if route.audience else None
                )
            ),
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
