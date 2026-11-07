"""HTTP origins whose credential-holding forwarder authenticates with its SVID."""

from agentplane.egress.sidecar_config import RUNNER_TLS_MOUNT_PATH, WorkloadRoute
from agentplane.environment_runtime_transport.config import TLS
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.service_ref import ServiceRef


def sidecar_routes(namespace: str, *, rules_origin: ServiceRef | None = None) -> dict[str, WorkloadRoute]:
    api = rules_origin or egress.agent_api(namespace)
    authority = f"{api.fqdn}:{api.port.number}"
    return {
        authority: WorkloadRoute(
            upstream=authority,
            server_uri="spiffe://agentplane/egress-proxy",
            tls=TLS(
                cert_file=f"{RUNNER_TLS_MOUNT_PATH}/tls.crt",
                key_file=f"{RUNNER_TLS_MOUNT_PATH}/tls.key",
                ca_file=f"{RUNNER_TLS_MOUNT_PATH}/ca.crt",
            ),
        )
    }
