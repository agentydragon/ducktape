"""Stable identity and addresses for the optional central code-mode sampler."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "central-sampler"
HEALTH_PORT = 8081
PORT = 8083
_LABELS = {"app.kubernetes.io/name": NAME}


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc-tls", number=PORT),
        pods=Pods(namespace=namespace, labels=tuple(_LABELS.items())),
    )


def pods(namespace: str) -> Pods:
    return service(namespace).pods
