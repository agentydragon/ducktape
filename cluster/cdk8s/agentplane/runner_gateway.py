"""Stable public Runner Gateway Service address."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "runner-gateway"
TOKEN_AUDIENCE = "agentplane-runner-gateway"
_LABELS = {"app.kubernetes.io/name": NAME}

GATEWAY_TLS_SECRET = "agentplane-runner-gateway-tls"
GATEWAY_TLS_MOUNT_PATH = "/run/agentplane/runner-tls"
RUNNER_CERTIFICATE_ISSUER = "agentplane-runner-ca"


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
