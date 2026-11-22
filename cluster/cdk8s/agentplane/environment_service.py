"""The every-replica EnvironmentService endpoint on the Sandbox Controller."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "environments"


def environments_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc", number=8082),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )
