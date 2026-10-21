"""The checked-in golden vectors are what this service decides, so the Go policies they pin are held to it."""

import json

import pytest_bazel

from agentplane.action_service.policies.generate_golden import build, serialize
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/action_service/policies/golden.json"


async def test_golden_vectors_are_what_the_python_service_decides() -> None:
    checked_in = json.loads(get_required_path(GOLDEN).read_text(encoding="utf-8"))
    produced = json.loads(serialize(await build()))
    hint = "regenerate with: bb run //agentplane/action_service/policies:generate_golden_bin -- <path to golden.json>"
    # Vector by vector, so a failure names the one that changed.
    for section, vectors in produced.items():
        for name, vector in vectors.items():
            assert checked_in.get(section, {}).get(name) == vector, f"{section}: {name}; {hint}"
    assert checked_in == produced, hint


if __name__ == "__main__":
    pytest_bazel.main()
