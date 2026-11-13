"""Haku's non-Sandbox workloads: their standing SVIDs, the Identity those act as, and their admission
to the egress proxy.

The Pods keep running as the `haku-ui` ServiceAccount, which the proxy's network policy selects; the
proxy never asserts a caller to platform services.
"""

from cdk8s import ApiObjectMetadata
from constructs import Construct

from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.providers.agentplane.identity import Identity
from cluster.cdk8s.providers.agentplane.workload_credential import WorkloadCredential
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy

NAMESPACE = "haku-sandbox"
SERVICE_ACCOUNT = "haku-ui"
# The Identity every standing record below names, in the policy namespace; its PolicyBinding
# (agentplane/actions_staging_policies.py) is what Haku's workloads may reach.
IDENTITY = "haku-ui"
# Haku's own workloads in haku-state's `k8s/`, each named after its StatefulSet or Deployment. Anki
# and Jupyter share haku-ui's account and Identity, but each has a record of its own.
STANDING_WORKLOADS = ("haku-ui", "haku-anki", "haku-jupyter")


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


def add_workload_credentials(scope: Construct, *, policy_namespace: str) -> Identity:
    """The Identity Haku's workloads act as, and one standing record per workload naming it, rendered
    here because Haku's deployer Role cannot write one (cluster/cdk8s/haku/workloads.md). haku-state's
    `k8s/` mounts each record's Secret, `svid-<name>`, into that workload's egress sidecar alone."""
    identity = Identity(
        scope,
        "haku-ui-identity",
        metadata=ApiObjectMetadata(name=IDENTITY, namespace=policy_namespace),
        description="Haku's operator UI, haku-anki and haku-jupyter, in haku-sandbox.",
    )
    for name in STANDING_WORKLOADS:
        WorkloadCredential(
            scope,
            f"workload-credential-{name}",
            metadata=ApiObjectMetadata(
                name=name,
                namespace=NAMESPACE,
                annotations={"description": f"The SVID {name}'s egress sidecar presents to the egress proxy."},
            ),
            subject=identity,
        )
    return identity
