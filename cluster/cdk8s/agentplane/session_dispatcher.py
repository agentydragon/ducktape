"""The stable sender Service targets the unified Runner Gateway listener."""

from cluster.cdk8s.agentplane import runner_gateway
from cluster.cdk8s.service_ref import Port, ServiceRef

NAME = "session-dispatcher"
TOKEN_AUDIENCE = NAME
WORKLOAD_CREDENTIAL = "agentplane-session-dispatcher-workload"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(name=NAME, port=Port(name="grpc", number=8080), pods=runner_gateway.service(namespace).pods)


def http_service(namespace: str) -> ServiceRef:
    """The Dispatcher's generated HTTP/JSON facade (agentplane/docs/grpc_services.md § HTTP/JSON facades),
    on the Gateway's second listener."""
    return ServiceRef(name=NAME, port=Port(name="http", number=8081), pods=runner_gateway.service(namespace).pods)
