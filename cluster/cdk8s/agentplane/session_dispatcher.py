"""The stable sender Service targets the unified Runner Gateway listener."""

from cluster.cdk8s.agentplane import runner_gateway
from cluster.cdk8s.service_ref import Port, ServiceRef

NAME = "session-dispatcher"
TOKEN_AUDIENCE = NAME


def service(namespace: str) -> ServiceRef:
    return ServiceRef(name=NAME, port=Port(name="grpc", number=8080), pods=runner_gateway.service(namespace).pods)
