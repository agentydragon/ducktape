"""`NetworkPolicy` refuses toFQDNs egress that Cilium's DNS proxy cannot see resolve."""

import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting
from cilium_crds.io.cilium import CiliumNetworkPolicySpecEgress

from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy, dns_egress, fqdn_fence

_DNS_LABELS = {"k8s-app": "test-dns"}


def _policy(egress: list[CiliumNetworkPolicySpecEgress]) -> None:
    NetworkPolicy(
        Cdk8sTesting.chart(),
        "policy",
        metadata=ApiObjectMetadata(name="test-policy", namespace="test-namespace"),
        selector={"app.kubernetes.io/name": "test-app"},
        egress=egress,
    )


# The first case is google-mcp's shape before it gained a DNS rule: plain port-53 egress admits
# the lookups but leaves them unproxied.
@pytest.mark.parametrize(
    "egress",
    [[dns_egress(_DNS_LABELS), EgressRule.to_fqdns("api.example.test")], [EgressRule.to_fqdns("api.example.test")]],
)
def test_fqdn_egress_without_dns_rule_is_refused(egress: list[CiliumNetworkPolicySpecEgress]) -> None:
    with pytest.raises(ValueError, match="DNS rule"):
        _policy(egress)


@pytest.mark.parametrize(
    "egress",
    [
        [dns_egress(_DNS_LABELS, resolves=["*"]), EgressRule.to_fqdns("api.example.test")],
        fqdn_fence(_DNS_LABELS, ["api.example.test"]),
        [dns_egress(_DNS_LABELS), EgressRule.to_entities("kube-apiserver")],
    ],
)
def test_dns_rule_or_no_fqdn_egress_is_accepted(egress: list[CiliumNetworkPolicySpecEgress]) -> None:
    _policy(egress)


if __name__ == "__main__":
    pytest_bazel.main()
