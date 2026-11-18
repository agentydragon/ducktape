"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from agentplane.egress.sidecar_config import WorkloadRoute
from agentplane.net.peertls.config import ACTIONS_URI, LLM_INGRESS_URI, SESSION_DISPATCHER_URI
from cluster.cdk8s.agentplane import actions, llm_ingress, runner_gateway


def sidecar_routes(namespace: str) -> dict[str, WorkloadRoute]:
    sessions = runner_gateway.sessions_service(namespace)
    action_origin = actions.service(namespace)
    action_destination = actions.service(namespace)
    llm_origin = llm_ingress.service(namespace)
    llm_destination = llm_ingress.service(namespace)
    return {
        f"{action_origin.fqdn}:{action_origin.port.number}": WorkloadRoute(
            upstream=f"{action_destination.fqdn}:{action_destination.port.number}", server_uri=ACTIONS_URI
        ),
        f"{llm_origin.fqdn}:{llm_origin.port.number}": WorkloadRoute(
            upstream=f"{llm_destination.fqdn}:{llm_destination.port.number}", server_uri=LLM_INGRESS_URI
        ),
        f"{sessions.fqdn}:{sessions.port.number}": WorkloadRoute(
            upstream=f"{sessions.fqdn}:{sessions.port.number}", server_uri=SESSION_DISPATCHER_URI
        ),
    }
