"""The every-replica EnvironmentService endpoint on the Sandbox Controller."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "sandbox-controller"
ENVIRONMENTS_ROUTING_LABELS = {"app.kubernetes.io/component": "environments"}


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc", number=8082),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )


def control_target(namespace: str) -> str:
    """Runtime proxy target, parameterized by the destination Sandbox namespace."""
    return _control_target(service(namespace))


def environments_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name="environments",
        port=Port(name="grpc", number=8082),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", "environments"),)),
    )


def environments_control_target(namespace: str) -> str:
    return _control_target(environments_service(namespace))


def _control_target(endpoint: ServiceRef) -> str:
    return f"{endpoint.name}.{{namespace}}.svc:{endpoint.port.number}"
