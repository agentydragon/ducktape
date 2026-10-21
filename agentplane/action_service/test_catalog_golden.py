"""The checked-in catalog vectors are what this service accepts and holds, so the Go catalog they pin is held to it."""

import pytest_bazel

from agentplane.action_service.generate_catalog_golden import build
from agentplane.action_service.generate_golden import serialize
from util.bazel.runfiles import get_required_path

GOLDEN = "_main/agentplane/action_service/catalog/golden.json"


def test_catalog_vectors_are_what_the_python_service_accepts() -> None:
    assert get_required_path(GOLDEN).read_text(encoding="utf-8") == serialize(build()), (
        "regenerate per generate_catalog_golden.py"
    )


if __name__ == "__main__":
    pytest_bazel.main()
