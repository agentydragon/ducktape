"""The shared construct gives only the egress proxy access and copies in no credential; each
environment's own module decides which credentials it gets."""

import pytest_bazel
from cdk8s import Chart

from cluster.cdk8s.agent_rbac_test_support import Permission, Rbac
from cluster.cdk8s.agentplane.egress_credentials import EgressCredentials

_CREDENTIALS_NAMESPACE = "test-egress-credentials"
_PROXY_NAMESPACE = "test-proxy"


def test_only_the_proxy_reads_and_nothing_is_copied(scratch_chart: Chart) -> None:
    EgressCredentials(scratch_chart, "credentials", namespace=_CREDENTIALS_NAMESPACE, proxy_namespace=_PROXY_NAMESPACE)
    objects = scratch_chart.to_json()
    assert not any(obj["kind"] in {"ExternalSecret", "ClusterSecretStore", "ServiceAccount"} for obj in objects)
    rbac = Rbac(objects)
    (binding,) = rbac.bindings
    assert binding["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "egress", "namespace": _PROXY_NAMESPACE}
    ]
    assert rbac.identity("ServiceAccount", "egress", _PROXY_NAMESPACE) == {
        Permission(_CREDENTIALS_NAMESPACE, "", "secrets", verb) for verb in ("get", "list", "watch")
    }


if __name__ == "__main__":
    pytest_bazel.main()
