"""The checked-in golden vectors are what this service shows, so the Go policy view they pin is held to it."""

import json

import pytest_bazel

from agentplane.action_service.policyview.generate_golden import build, serialize
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/action_service/policyview/golden.json"


def test_golden_vectors_are_what_the_python_service_shows() -> None:
    checked_in = json.loads(get_required_path(GOLDEN).read_text(encoding="utf-8"))
    produced = json.loads(serialize(build()))
    hint = "regenerate with: bb run //agentplane/action_service/policyview:generate_golden_bin -- <path to golden.json>"
    # Vector by vector, so a failure names the one that changed.
    for name, vector in produced["views"].items():
        assert checked_in["views"].get(name) == vector, f"{name}; {hint}"
    assert checked_in == produced, hint


if __name__ == "__main__":
    pytest_bazel.main()
