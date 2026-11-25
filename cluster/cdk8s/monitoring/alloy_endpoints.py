"""Workload identities and OTLP Service ports for the Alloy Helm releases."""

from cluster.cdk8s.monitoring.namespace import NAMESPACE
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

CENTRAL_NAME = "alloy"
NODE_NAME = "alloy-node"
JOURNAL_NAME = "alloy-journal"
SYSLOG_NAME = "alloy-syslog"


def _pods(name: str) -> Pods:
    # The Helm chart sets this label from the release name or its nameOverride.
    return Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", name),))


CENTRAL = _pods(CENTRAL_NAME)
NODE = _pods(NODE_NAME)
JOURNAL = _pods(JOURNAL_NAME)
SYSLOG = _pods(SYSLOG_NAME)

OTLP_GRPC = ServiceRef(name=CENTRAL_NAME, port=Port(name="otlp-grpc", number=4317), pods=CENTRAL)
OTLP_HTTP = ServiceRef(name=CENTRAL_NAME, port=Port(name="otlp-http", number=4318), pods=CENTRAL)
OTLP_GRPC_ENDPOINT = f"http://{OTLP_GRPC.authority}"
OTLP_HTTP_ENDPOINT = f"http://{OTLP_HTTP.authority}"
