"""The checked-in sandbox vectors are what this service offers and accepts, so the Go sandbox Actions they pin are
held to it."""

import pytest_bazel

from agentplane.action_service.generate_golden import serialize
from agentplane.action_service.sandbox.generate_golden import build
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/action_service/sandbox/golden.json"


def test_sandbox_vectors_are_what_the_python_service_offers() -> None:
    assert get_required_path(GOLDEN).read_text(encoding="utf-8") == serialize(build()), (
        "regenerate per generate_golden.py"
    )


if __name__ == "__main__":
    pytest_bazel.main()
