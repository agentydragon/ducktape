import pytest_bazel

from agentplane.proto import runner_pb2
from agentplane.proto.sandbox_service_pb2 import SandboxBinding, SessionDefaults
from agentplane.runner_gateway.session_lifecycle import launch_spec

# gazelle:include_dep @pypi//protobuf


def test_binding_default_cwd_names_the_session() -> None:
    spec = launch_spec(
        "test-session",
        {},
        binding=SandboxBinding(
            session_defaults=SessionDefaults(
                harness=runner_pb2.HARNESS_CODEX, model="test-model", cwd="/state/{session_id}"
            )
        ),
    )
    assert spec.cwd == "/state/test-session"


if __name__ == "__main__":
    pytest_bazel.main()
