"""The every-replica EnvironmentService endpoint on the Sandbox Controller."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "environments"
ENVIRONMENTS_ROUTING_LABELS = {"app.kubernetes.io/component": "environments"}


def environments_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc", number=8082),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )


def environments_control_target(namespace: str) -> str:
    endpoint = environments_service(namespace)
    return f"{endpoint.name}.{{namespace}}.svc:{endpoint.port.number}"
