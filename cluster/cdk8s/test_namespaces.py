import pytest
import pytest_bazel
from cdk8s import Testing as Cdk8sTesting  # pytest auto-collects classes named Test*

from cluster.cdk8s.namespaces import AgentReadable, Vpa, namespace


def test_extra_labels_cannot_override_the_policy_labels() -> None:
    with pytest.raises(ValueError, match="agent-readable-logs"):
        namespace(
            Cdk8sTesting.chart(),
            "namespace",
            name="test-ns",
            vpa=Vpa.AUTO,
            agent_readable=AgentReadable.LOGS,
            labels={AgentReadable.LOGS: "false"},
        )


if __name__ == "__main__":
    pytest_bazel.main()
