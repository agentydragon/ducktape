"""HTTP origins whose Sandbox sidecar presents its SVID to the service itself."""

from pathlib import Path

from agentplane.egress import sidecar_config
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.service_ref import ServiceRef


def rules_route(namespace: str, *, origin: ServiceRef | None = None) -> dict[str, sidecar_config.WorkloadRoute]:
    """Send the agent-facing rules origin to its TLS-only, Sandbox-authenticated service."""
    origin = origin or egress.agent_api(namespace)
    destination = egress.rules_workload_service(namespace)
    certificate_path = Path(sidecar_config.RUNNER_TLS_MOUNT_PATH)
    return {
        f"{origin.fqdn}:{origin.port.number}": sidecar_config.WorkloadRoute(
            upstream=f"{destination.fqdn}:{destination.port.number}",
            server_uri=egress.RULES_WORKLOAD_URI,
            tls=TLS(
                cert_file=str(certificate_path / "tls.crt"),
                key_file=str(certificate_path / "tls.key"),
                ca_file=str(certificate_path / "ca.crt"),
            ),
        )
    }
