"""An unreviewed namespace gets no static diagnostics access."""

import pytest
import pytest_bazel
from cdk8s import App, Chart

from cluster.cdk8s import agent_namespace_rbac
from cluster.cdk8s.namespace_access import AgentReadable
from cluster.cdk8s.namespaces import Vpa, namespace


def test_unapproved_namespaces_fail_closed(scratch_chart: Chart, scratch_app: App) -> None:
    with pytest.raises(KeyError):
        agent_namespace_rbac.chart(scratch_app, "private-unreviewed")
    ns = namespace(scratch_chart, "namespace", name="private-unreviewed", vpa=Vpa.AUTO)
    labels = ns.metadata.to_json()["labels"]
    assert not set(labels) & set(AgentReadable)


if __name__ == "__main__":
    pytest_bazel.main()
