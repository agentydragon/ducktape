"""The checked-in golden vectors are what this fold produces, so the Go fold they pin is held to it."""

import json

import pytest_bazel

from agentplane.history_service.fold.generate_golden import build, serialize
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/history_service/threadfold/golden.json"


def test_golden_vectors_are_what_the_python_fold_produces() -> None:
    checked_in = get_required_path(GOLDEN).read_text(encoding="utf-8")
    produced = serialize(build())
    # Scenario by scenario first, so a failure names the one that changed.
    for stored, made in zip(json.loads(checked_in)["scenarios"], json.loads(produced)["scenarios"], strict=True):
        assert stored == made, stored["name"]
    assert checked_in == produced, "regenerate with: bb run //agentplane/history_service/fold:generate_golden_bin"


if __name__ == "__main__":
    pytest_bazel.main()
