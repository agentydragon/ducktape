"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from pathlib import Path

from agentplane.egress.sidecar_config import RUNNER_TLS_MOUNT_PATH, WorkloadRoute
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import egress, notifications
from cluster.cdk8s.service_ref import ServiceRef


def sidecar_routes(namespace: str, *, rules_origin: ServiceRef | None = None) -> dict[str, WorkloadRoute]:
    rules_origin = rules_origin or egress.agent_api(namespace)
    rules_destination = egress.rules_workload_service(namespace)
    notification_origin = notifications.http_service(namespace)
    notification_destination = notifications.workload_service(namespace)
    certificate_path = Path(RUNNER_TLS_MOUNT_PATH)
    security = TLS(
        cert_file=str(certificate_path / "tls.crt"),
        key_file=str(certificate_path / "tls.key"),
        ca_file=str(certificate_path / "ca.crt"),
    )
    return {
        f"{rules_origin.fqdn}:{rules_origin.port.number}": WorkloadRoute(
            upstream=f"{rules_destination.fqdn}:{rules_destination.port.number}",
            server_uri=egress.RULES_WORKLOAD_URI,
            tls=security,
        ),
        f"{notification_origin.fqdn}:{notification_origin.port.number}": WorkloadRoute(
            upstream=f"{notification_destination.fqdn}:{notification_destination.port.number}",
            server_uri=notifications.WORKLOAD_URI,
            tls=security,
        ),
    }
