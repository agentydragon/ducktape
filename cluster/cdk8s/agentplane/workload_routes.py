"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from pathlib import Path

from agentplane.egress.sidecar_config import RUNNER_TLS_MOUNT_PATH, WorkloadRoute
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import actions, notifications


def sidecar_routes(namespace: str) -> dict[str, WorkloadRoute]:
    origin = notifications.http_service(namespace)
    destination = notifications.workload_service(namespace)
    # The controller and launcher injection mount the same Runner Secret into
    # Runtime and its credential-holding relay; the workload never receives it.
    certificate_path = Path(RUNNER_TLS_MOUNT_PATH)
    return {
        f"{origin.fqdn}:{origin.port.number}": WorkloadRoute(
            upstream=f"{destination.fqdn}:{destination.pod_port}",
            server_uri=notifications.WORKLOAD_URI,
            tls=TLS(
                cert_file=str(certificate_path / "tls.crt"),
                key_file=str(certificate_path / "tls.key"),
                ca_file=str(certificate_path / "ca.crt"),
            ),
        ),
        f"{actions.service(namespace).fqdn}:{actions.service(namespace).port.number}": WorkloadRoute(
            upstream=f"{actions.workload_service(namespace).fqdn}:{actions.workload_service(namespace).pod_port}",
            server_uri=actions.WORKLOAD_URI,
            tls=TLS(
                cert_file=str(certificate_path / "tls.crt"),
                key_file=str(certificate_path / "tls.key"),
                ca_file=str(certificate_path / "ca.crt"),
            ),
        ),
    }
