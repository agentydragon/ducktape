"""Static namespace readers wait only on the shared roles, and an unreviewed namespace gets none."""

import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind

from cluster.cdk8s import agent_namespace_rbac
from cluster.cdk8s.flux import flux_kustomization, kustomizations_chart
from cluster.cdk8s.namespace_access import NAMESPACE_DIAGNOSTICS, AgentReadable
from cluster.cdk8s.namespaces import Vpa, namespace


def test_reader_reconciliation_depends_only_on_shared_roles() -> None:
    # No namespace-owner or application constructs are needed, including on a
    # cold bootstrap. Namespace existence is checked by the API, not app health.
    scope = kustomizations_chart(Cdk8sTesting.app())
    roles = flux_kustomization(
        scope,
        "shared-roles",
        KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape"),
        path="./roles",
    )
    agent_namespace_rbac.add_flux_kustomizations(scope, roles)
    readers = [doc for doc in Cdk8sTesting.synth(scope) if doc["metadata"]["name"] != roles.name]
    assert {doc["metadata"]["name"] for doc in readers} == {
        f"agent-namespace-rbac-{name}" for name in NAMESPACE_DIAGNOSTICS
    }
    for doc in readers:
        name = doc["metadata"]["name"].removeprefix("agent-namespace-rbac-")
        spec = doc["spec"]
        assert spec["dependsOn"] == [{"name": roles.name, "namespace": "ducktape-flux"}]
        assert spec["path"] == f"./{agent_namespace_rbac.directory(name)}"
        assert spec["sourceRef"] == {"kind": "GitRepository", "name": "ducktape", "namespace": "ducktape-flux"}
        assert spec["retryInterval"] == "1m"
        assert spec["prune"] is True
        assert spec["wait"] is True


def test_unapproved_namespaces_fail_closed() -> None:
    with pytest.raises(KeyError):
        agent_namespace_rbac.chart(Cdk8sTesting.app(), "private-unreviewed")
    scope = Cdk8sTesting.chart()
    namespace(scope, "namespace", name="private-unreviewed", vpa=Vpa.AUTO)
    labels = Cdk8sTesting.synth(scope)[0]["metadata"]["labels"]
    assert not set(labels) & set(AgentReadable)


if __name__ == "__main__":
    pytest_bazel.main()
