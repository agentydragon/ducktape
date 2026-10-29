"""The shared construct gives only the egress proxy access and copies in no credential; each
environment's own module decides which credentials it gets."""

from pathlib import Path

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting

from cluster.cdk8s.agentplane.egress_credentials import EgressCredentials
from cluster.cdk8s.testing.scratch import scratch_chart

_CREDENTIALS_NAMESPACE = "test-egress-credentials"
_PROXY_NAMESPACE = "test-proxy"


def test_only_the_proxy_reads_and_nothing_is_copied(tmp_path: Path) -> None:
    chart = scratch_chart(tmp_path)
    EgressCredentials(chart, "credentials", namespace=_CREDENTIALS_NAMESPACE, proxy_namespace=_PROXY_NAMESPACE)
    objects = Cdk8sTesting.synth(chart)
    assert not any(obj["kind"] in {"ExternalSecret", "ClusterSecretStore", "ServiceAccount"} for obj in objects)
    bindings = [obj for obj in objects if obj["kind"] == "RoleBinding"]
    assert len(bindings) == 1
    assert bindings[0]["metadata"]["name"] == "egress"
    assert bindings[0]["metadata"]["namespace"] == _CREDENTIALS_NAMESPACE
    assert bindings[0]["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "egress-credentials-reader",
    }
    assert bindings[0]["subjects"] == [
        {"apiGroup": "", "kind": "ServiceAccount", "name": "egress", "namespace": _PROXY_NAMESPACE}
    ]


if __name__ == "__main__":
    pytest_bazel.main()
