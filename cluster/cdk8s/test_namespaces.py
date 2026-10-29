from pathlib import Path

import pytest
import pytest_bazel

from cluster.cdk8s.namespace_access import AgentReadable
from cluster.cdk8s.namespaces import Vpa, namespace
from cluster.cdk8s.testing.scratch import scratch_chart


@pytest.mark.parametrize("key", [AgentReadable.LOGS, "goldilocks.fairwinds.com/enabled"])
def test_extra_labels_cannot_restate_the_policy_labels(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError, match=key):
        namespace(scratch_chart(tmp_path), "namespace", name="test-ns", vpa=Vpa.AUTO, labels={key: "false"})


if __name__ == "__main__":
    pytest_bazel.main()
