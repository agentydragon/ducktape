"""Stable Runner Gateway addresses and runner-control egress identity."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "runner-gateway"
TOKEN_AUDIENCE = "agentplane-runner-gateway"
_LABELS = {"app.kubernetes.io/name": NAME}

# The policy a runner behind the egress proxy dials the control stream under (the KubeVirt
# experiment's; a Sandbox's runner uses its sidecar's platform route), and the credential it substitutes.
RUNNER_CONTROL_POLICY = "runner-control"
RUNNER_CONTROL_CREDENTIAL = "agentplane-runner-control"


def service(namespace: str) -> ServiceRef:
    return ServiceRef(
        name=NAME, port=Port(name="grpc", number=8080), pods=Pods(namespace=namespace, labels=tuple(_LABELS.items()))
    )


def target(namespace: str) -> str:
    return f"{service(namespace).fqdn}:{service(namespace).pod_port}"


def control_target(namespace: str) -> str:
    """Each Sandbox's control Service, `<sandbox>-control`, which names the replica holding its runner
    connection, as the `{sandbox}`/`{namespace}` template the clients' `control_target` takes."""
    return f"{{sandbox}}-control.{{namespace}}.svc:{service(namespace).pod_port}"


def runner_control(namespace: str) -> ServiceRef:
    """The listener runners dial out to (agentplane/docs/runner_control.md), through the egress proxy."""
    return ServiceRef(
        name=NAME,
        port=Port(name="runner-control", number=8082),
        pods=Pods(namespace=namespace, labels=tuple(_LABELS.items())),
    )
