"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from pathlib import Path

from agentplane.egress.sidecar_config import RUNNER_TLS_MOUNT_PATH, WorkloadRoute
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import actions, egress, llm_ingress, notifications, runner_gateway, session_dispatcher
from cluster.cdk8s.service_ref import ServiceRef


def sidecar_routes(namespace: str, *, rules_origin: ServiceRef | None = None) -> dict[str, WorkloadRoute]:
    rules_origin = rules_origin or egress.agent_api(namespace)
    rules_destination = rules_origin
    notification_origin = notifications.http_service(namespace)
    notification_destination = notifications.workload_service(namespace)
    dispatcher_origin = session_dispatcher.service(namespace)
    dispatcher_http_origin = session_dispatcher.http_service(namespace)
    dispatcher_destination = runner_gateway.workload_service(namespace)
    action_origin = actions.service(namespace)
    action_destination = actions.workload_service(namespace)
    llm_origin = llm_ingress.service(namespace)
    llm_destination = llm_ingress.workload_service(namespace)
    certificate_path = Path(RUNNER_TLS_MOUNT_PATH)
    security = TLS(
        cert_file=str(certificate_path / "tls.crt"),
        key_file=str(certificate_path / "tls.key"),
        ca_file=str(certificate_path / "ca.crt"),
    )
    dispatcher = WorkloadRoute(
        upstream=f"{dispatcher_destination.fqdn}:{dispatcher_destination.port.number}",
        server_uri=runner_gateway.WORKLOAD_URI,
        tls=security,
    )
    return {
        f"{rules_origin.fqdn}:{rules_origin.port.number}": WorkloadRoute(
            upstream=f"{rules_destination.fqdn}:{rules_destination.port.number}",
            server_uri="spiffe://agentplane/egress-proxy",
            tls=security,
        ),
        f"{notification_origin.fqdn}:{notification_origin.port.number}": WorkloadRoute(
            upstream=f"{notification_destination.fqdn}:{notification_destination.port.number}",
            server_uri=notifications.WORKLOAD_URI,
            tls=security,
        ),
        f"{action_origin.fqdn}:{action_origin.port.number}": WorkloadRoute(
            upstream=f"{action_destination.fqdn}:{action_destination.port.number}",
            server_uri=actions.WORKLOAD_URI,
            tls=security,
        ),
        f"{llm_origin.fqdn}:{llm_origin.port.number}": WorkloadRoute(
            upstream=f"{llm_destination.fqdn}:{llm_destination.port.number}",
            server_uri=llm_ingress.WORKLOAD_URI,
            tls=security,
        ),
        f"{dispatcher_origin.fqdn}:{dispatcher_origin.port.number}": dispatcher,
        f"{dispatcher_http_origin.fqdn}:{dispatcher_http_origin.port.number}": dispatcher,
    }
