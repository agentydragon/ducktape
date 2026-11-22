"""The every-replica EnvironmentService endpoint on the Sandbox Controller."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "environments"
_GRPC = Port(name="grpc", number=8082)
# Clients substitute the destination namespace when dialing its receiver.
CONTROL_TARGET_TEMPLATE = f"{NAME}.{{namespace}}.svc:{_GRPC.number}"
ENVIRONMENTS_ROUTING_LABELS = {"app.kubernetes.io/component": "environments"}


def environments_service(namespace: str) -> ServiceRef:
    return ServiceRef(name=NAME, port=_GRPC, pods=Pods(namespace=namespace, labels=(("app.kubernetes.io/name", NAME),)))
