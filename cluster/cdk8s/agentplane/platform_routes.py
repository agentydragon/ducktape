"""The Agentplane services runners call over the egress sidecar's platform routes
(agentplane/egress/platform_routes.py). From this one list come:
- the sidecar's routes and the runner's endpoint flags in the runner SandboxTemplates;
- the network rules on both ends.

Each route uses the token the sidecar already projects for its service's audience (sandbox_pod.py).
Neither an egress policy nor the central proxy is involved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import k8s
from constructs import Construct

from agentplane.egress import sidecar
from agentplane.egress.platform_routes import PlatformRoute
from agentplane.history_service.settings import RUNNER_INGRESS_AUDIENCE
from agentplane.runner_gateway.settings import RUNNER_CONTROL_AUDIENCE
from cluster.cdk8s.agentplane import history_service, runner_gateway, sandbox_pod
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from cluster.cdk8s.service_ref import Pods, ServiceRef
from util.settings_contract import env_name


@dataclass(frozen=True)
class Route:
    name: str
    service: ServiceRef
    audience: str
    # On the sidecar's loopback, beside its proxy (3128) and readiness (3129) ports.
    listen_port: int

    @property
    def local_target(self) -> str:
        return f"127.0.0.1:{self.listen_port}"


# Each route replaces `authorization` with its own token, so a runner presents this placeholder.
ROUTE_BEARER = "platform-route"


def control(namespace: str) -> Route:
    return Route("control", runner_gateway.runner_control(namespace), RUNNER_CONTROL_AUDIENCE, 3130)


def ingress(namespace: str) -> Route:
    return Route("ingress", history_service.ingress_service(namespace), RUNNER_INGRESS_AUDIENCE, 3131)


def routes(namespace: str) -> tuple[Route, ...]:
    return control(namespace), ingress(namespace)


def sidecar_env(namespace: str) -> k8s.EnvVar:
    """The sidecar's `platform_routes` setting, as its environment variable."""
    value = {
        route.name: PlatformRoute(
            listen_port=route.listen_port,
            upstream=f"{route.service.fqdn}:{route.service.pod_port}",
            token_file=Path(sandbox_pod.audience_token_file(route.audience)),
        ).model_dump(mode="json")
        for route in routes(namespace)
    }
    return k8s.EnvVar(name=env_name(sidecar.Settings, "platform_routes"), value=json.dumps(value))


def runner_args(namespace: str) -> list[str]:
    control_route, ingress_route = control(namespace), ingress(namespace)
    return [
        "--control-target",
        control_route.local_target,
        "--control-bearer",
        ROUTE_BEARER,
        "--ingress-target",
        ingress_route.local_target,
        "--ingress-bearer",
        ROUTE_BEARER,
    ]


class PlatformRouteFences(Construct):
    """`clients` may reach each route's service directly: an egress rule on them and an ingress rule
    on the service. Cilium unions these with each side's own policy."""

    def __init__(self, scope: Construct, id: str, *, namespace: str, clients: Pods) -> None:
        super().__init__(scope, id)
        NetworkPolicy(
            self,
            "clients",
            metadata=ApiObjectMetadata(name="agentplane-platform-route-clients", namespace=namespace),
            endpoint_selector=clients.selector,
            egress=[route.service.egress() for route in routes(namespace)],
        )
        for route in routes(namespace):
            NetworkPolicy(
                self,
                route.name,
                metadata=ApiObjectMetadata(name=f"agentplane-platform-route-{route.name}", namespace=namespace),
                endpoint_selector=route.service.pods.selector,
                ingress=[clients.admit(route.service.pod_port)],
            )
