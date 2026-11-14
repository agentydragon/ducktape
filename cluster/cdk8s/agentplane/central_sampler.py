"""Stable identity and addresses for the optional central code-mode sampler."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "central-sampler"
TOKEN_AUDIENCE = NAME
PORT = 8080
TLS_PORT = 8083
_LABELS = {"app.kubernetes.io/name": NAME}


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=PORT), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def tls_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc-tls", number=TLS_PORT),
        pods=Pods(namespace=namespace, labels=tuple(_LABELS.items())),
    )


def pods(namespace: str) -> Pods:
    return service(namespace).pods


def target(namespace: str) -> str:
    endpoint = service(namespace)
    return f"{endpoint.fqdn}:{endpoint.pod_port}"
