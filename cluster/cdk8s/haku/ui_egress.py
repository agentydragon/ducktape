"""Haku's non-Sandbox workloads: their standing SVIDs and their admission to the egress proxy.

The account and policy bindings remain unchanged; the proxy never asserts a caller to platform services.
"""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ServiceAccount
from constructs import Construct

from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.providers.agentplane.workload_credential import WorkloadCredential
from cluster.cdk8s.providers.cilium.network_policy import IngressRule, NetworkPolicy

NAMESPACE = "haku-sandbox"
SERVICE_ACCOUNT = "haku-ui"
JUPYTER_SERVICE_ACCOUNT = "haku-jupyter"
# Haku's own workloads in haku-state's `k8s/`, each named after its StatefulSet or Deployment, and the
# account it runs as. Anki shares haku-ui's account and grants, but has a record of its own; Jupyter
# has its own account, so its rules and decision history stay apart from haku-ui's.
STANDING_WORKLOADS = {"haku-ui": SERVICE_ACCOUNT, "haku-anki": SERVICE_ACCOUNT, "haku-jupyter": JUPYTER_SERVICE_ACCOUNT}


def add_gateway_resources(scope: Construct, *, namespace: str) -> None:
    """Admit only these accounts to the proxy; their PolicyBindings authorize requests."""
    gateway = egress.proxy(namespace)
    NetworkPolicy(
        scope,
        "haku-workloads-gateway-network",
        metadata=ApiObjectMetadata(name="haku-workloads", namespace=namespace),
        endpoint_selector=gateway.pods.selector,
        ingress=[
            IngressRule.from_endpoints(
                *(
                    {"k8s:io.kubernetes.pod.namespace": NAMESPACE, "io.cilium.k8s.policy.serviceaccount": account}
                    for account in sorted(set(STANDING_WORKLOADS.values()))
                ),
                ports=[gateway.pod_port],
            )
        ],
    )


def add_workload_credentials(scope: Construct) -> None:
    """One standing record per Haku workload, rendered here because Haku's deployer Role cannot write
    one (cluster/cdk8s/haku/workloads.md). haku-state's `k8s/` mounts its Secret, `svid-<name>`, into
    that workload's egress sidecar alone."""
    # cluster/cdk8s/haku/rbac.py declares the accounts.
    accounts = {
        account: ServiceAccount.from_service_account_name(scope, f"{account}-account-ref", account)
        for account in sorted(set(STANDING_WORKLOADS.values()))
    }
    for name, account in STANDING_WORKLOADS.items():
        WorkloadCredential(
            scope,
            f"workload-credential-{name}",
            metadata=ApiObjectMetadata(
                name=name,
                namespace=NAMESPACE,
                annotations={"description": f"The SVID {name}'s egress sidecar presents to the egress proxy."},
            ),
            subject=accounts[account],
        )
