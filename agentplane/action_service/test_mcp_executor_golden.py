"""The checked-in MCP executor vectors are what this service accepts, mirrors and keeps, so the Go executor they pin
is held to it."""

import pytest_bazel

from agentplane.action_service.generate_golden import serialize
from agentplane.action_service.generate_mcp_executor_golden import build
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/action_service/mcpexec/golden.json"


def test_mcp_executor_vectors_are_what_the_python_service_does() -> None:
    assert get_required_path(GOLDEN).read_text(encoding="utf-8") == serialize(build()), (
        "regenerate per generate_mcp_executor_golden.py"
    )


if __name__ == "__main__":
    pytest_bazel.main()
