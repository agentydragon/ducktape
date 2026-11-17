"""Stable public Runner Gateway Service address."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "runner-gateway"
_LABELS = {"app.kubernetes.io/name": NAME}
SESSIONS_ROUTING_LABELS = {"app.kubernetes.io/component": "sessions"}


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def target(namespace: str) -> str:
    return f"{service(namespace).fqdn}:{service(namespace).pod_port}"


def control_target(namespace: str) -> str:
    """Stable shared Runner Gateway Service target, parameterized by the Sandbox namespace."""
    return f"{NAME}.{{namespace}}.svc:{service(namespace).pod_port}"


def sessions_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name="sessions",
        port=Port(name="grpc", number=8080),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", "sessions"),)),
    )
