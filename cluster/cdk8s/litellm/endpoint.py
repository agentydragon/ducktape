"""The cluster LiteLLM proxy that serves API clients."""

from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAMESPACE = "litellm"
NAME = "litellm"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=4000),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
API_BASE = f"http://{SERVICE.authority}"
