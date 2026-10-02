import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.namespace_access import AgentReadable
from cluster.cdk8s.namespaces import Vpa, namespace


@pytest.mark.parametrize("key", [AgentReadable.LOGS, "goldilocks.fairwinds.com/enabled"])
def test_extra_labels_cannot_restate_the_policy_labels(key: str) -> None:
    with pytest.raises(ValueError, match=key):
        namespace(Cdk8sTesting.chart(), "namespace", name="test-ns", vpa=Vpa.AUTO, labels={key: "false"})


if __name__ == "__main__":
    pytest_bazel.main()
