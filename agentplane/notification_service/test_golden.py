"""The checked-in golden vectors are what this service produces, so the Go store they pin is held to it."""

import json

import pytest_bazel

from agentplane.notification_service.generate_golden import build, serialize
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/notification_service/store/golden.json"


def test_golden_vectors_are_what_the_python_service_produces() -> None:
    checked_in = get_required_path(GOLDEN).read_text(encoding="utf-8")
    produced = serialize(build())
    # Section by section first, so a failure names the one that changed.
    for section, stored in json.loads(checked_in).items():
        assert stored == json.loads(produced)[section], section
    assert checked_in == produced, "regenerate with: bb run //agentplane/notification_service:generate_golden_bin"


if __name__ == "__main__":
    pytest_bazel.main()
