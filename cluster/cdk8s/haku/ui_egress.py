"""Haku's non-Sandbox workloads retain their Pod-bound bearer proxy hop over verified TLS.

The account and policy bindings remain unchanged. Workload identity issuance independent of
Sandboxes is the later cutover; the proxy never asserts a caller to platform services.
"""

from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy

NAMESPACE = "haku-sandbox"
SERVICE_ACCOUNT = "haku-ui"


def add_gateway_resources(scope: Construct, *, namespace: str) -> None:
    """Admit only this existing account to the proxy; its PolicyBindings authorize requests."""
    gateway = egress.proxy(namespace)
    NetworkPolicy(
        scope,
        "haku-ui-gateway-network",
        metadata=ApiObjectMetadata(name="haku-ui", namespace=namespace),
        endpoint_selector=gateway.pods.selector,
        ingress=[
            IngressRule.from_endpoints(
                {"k8s:io.kubernetes.pod.namespace": NAMESPACE, "io.cilium.k8s.policy.serviceaccount": SERVICE_ACCOUNT},
                ports=[gateway.pod_port],
            )
        ],
    )
