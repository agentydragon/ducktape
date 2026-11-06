"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from pathlib import Path

from agentplane.egress.sidecar_config import RUNNER_TLS_MOUNT_PATH, WorkloadRoute
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import llm_ingress, notifications


def sidecar_routes(namespace: str) -> dict[str, WorkloadRoute]:
    # The controller and launcher injection mount the same Runner Secret into
    # Runtime and its credential-holding relay; the workload never receives it.
    certificate_path = Path(RUNNER_TLS_MOUNT_PATH)
    routes: dict[str, WorkloadRoute] = {}
    notification_origin = notifications.http_service(namespace)
    notification_destination = notifications.workload_service(namespace)
    routes[f"{notification_origin.fqdn}:{notification_origin.port.number}"] = WorkloadRoute(
        upstream=f"{notification_destination.fqdn}:{notification_destination.pod_port}",
        server_uri=notifications.WORKLOAD_URI,
        tls=TLS(
            cert_file=str(certificate_path / "tls.crt"),
            key_file=str(certificate_path / "tls.key"),
            ca_file=str(certificate_path / "ca.crt"),
        ),
    )
    llm_origin = llm_ingress.service(namespace)
    llm_destination = llm_ingress.workload_service(namespace)
    routes[f"{llm_origin.fqdn}:{llm_origin.port.number}"] = WorkloadRoute(
        upstream=f"{llm_destination.fqdn}:{llm_destination.pod_port}",
        server_uri=llm_ingress.WORKLOAD_URI,
        tls=TLS(
            cert_file=str(certificate_path / "tls.crt"),
            key_file=str(certificate_path / "tls.key"),
            ca_file=str(certificate_path / "ca.crt"),
        ),
    )
    return routes
