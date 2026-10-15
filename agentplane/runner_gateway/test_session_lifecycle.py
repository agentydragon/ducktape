import pytest
import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.runner_gateway.session_lifecycle import validate_spec

# gazelle:include_dep @pypi//protobuf


def test_launch_spec_requires_an_absolute_cwd() -> None:
    spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, model="test-model", cwd="relative")
    with pytest.raises(ValueError, match="cwd must be absolute"):
        validate_spec(spec)


if __name__ == "__main__":
    pytest_bazel.main()
