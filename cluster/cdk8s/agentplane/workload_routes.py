"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from pathlib import Path

from agentplane.egress.sidecar_config import RUNNER_TLS_MOUNT_PATH, WorkloadRoute
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import notifications, runner_gateway, session_dispatcher


def sidecar_routes(namespace: str) -> dict[str, WorkloadRoute]:
    origin = notifications.http_service(namespace)
    destination = notifications.workload_service(namespace)
    dispatcher_origin = session_dispatcher.service(namespace)
    dispatcher_http_origin = session_dispatcher.http_service(namespace)
    dispatcher_destination = runner_gateway.workload_service(namespace)
    # The controller and launcher injection mount the same Runner Secret into
    # Runtime and its credential-holding relay; the workload never receives it.
    certificate_path = Path(RUNNER_TLS_MOUNT_PATH)
    route_tls = TLS(
        cert_file=str(certificate_path / "tls.crt"),
        key_file=str(certificate_path / "tls.key"),
        ca_file=str(certificate_path / "ca.crt"),
    )
    return {
        f"{origin.fqdn}:{origin.port.number}": WorkloadRoute(
            upstream=f"{destination.fqdn}:{destination.pod_port}", server_uri=notifications.WORKLOAD_URI, tls=route_tls
        ),
        **{
            f"{endpoint.fqdn}:{endpoint.port.number}": WorkloadRoute(
                upstream=f"{dispatcher_destination.fqdn}:{dispatcher_destination.pod_port}",
                server_uri=runner_gateway.WORKLOAD_URI,
                tls=route_tls,
            )
            for endpoint in (dispatcher_origin, dispatcher_http_origin)
        },
    }
