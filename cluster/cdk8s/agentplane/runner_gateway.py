"""Sessions Service address."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "sessions"
SESSIONS_ROUTING_LABELS = {"app.kubernetes.io/component": "sessions"}


def sessions_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc", number=8080),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )
