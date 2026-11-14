"""Stable public Runner Gateway Service address."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "runner-gateway"
TOKEN_AUDIENCE = "agentplane-runner-gateway"
_LABELS = {"app.kubernetes.io/name": NAME}

GATEWAY_TLS_SECRET = "agentplane-runner-gateway-tls"
GATEWAY_TLS_MOUNT_PATH = "/run/agentplane/runner-tls"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def target(namespace: str) -> str:
    return f"{service(namespace).fqdn}:{service(namespace).pod_port}"


def control_target(namespace: str) -> str:
    """Stable shared Runner Gateway Service target, parameterized by the Sandbox namespace."""
    return f"{NAME}.{{namespace}}.svc:{service(namespace).pod_port}"
