"""The every-replica EnvironmentService endpoint on the Sandbox Controller."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "sandbox-controller"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME,
        port=Port(name="grpc", number=8082),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)),
    )


def control_target(namespace: str) -> str:
    """Runtime proxy target, parameterized by the destination Sandbox namespace."""
    return f"{NAME}.{{namespace}}.svc:{service(namespace).pod_port}"


def environments_service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name="environments",
        port=Port(name="grpc", number=8082),
        pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", "environments"),)),
    )
