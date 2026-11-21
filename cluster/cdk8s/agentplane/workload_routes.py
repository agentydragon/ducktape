"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from agentplane.egress.sidecar_config import WorkloadRoute
from agentplane.net.peertls.config import ACTIONS_URI, LLM_INGRESS_URI, SESSION_DISPATCHER_URI
from cluster.cdk8s.agentplane import actions, llm_ingress, runner_gateway


def sidecar_routes(namespace: str) -> dict[str, WorkloadRoute]:
    routes: dict[str, WorkloadRoute] = {}
    for service, server_uri in (
        (actions.service(namespace), ACTIONS_URI),
        (llm_ingress.service(namespace), LLM_INGRESS_URI),
        (runner_gateway.sessions_service(namespace), SESSION_DISPATCHER_URI),
    ):
        authority = f"{service.fqdn}:{service.port.number}"
        routes[authority] = WorkloadRoute(upstream=authority, server_uri=server_uri)
    return routes
