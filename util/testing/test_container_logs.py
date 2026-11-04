"""Tests for LoggedContainer log persistence."""

from pathlib import Path

import pytest
import pytest_bazel

from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from third_party.containers import debian_slim
from util.testing.container_logs import LoggedContainer


@pytest.fixture(scope="module")
def image_tag() -> str:
    """Load debian-slim and return its tag."""
    return load_oci_image(debian_slim.LAYOUT_RLOCATION)


def _logs_dir(test_name: str) -> Path:
    return undeclared_outputs_dir() / test_name


def test_logs_collected_on_success(image_tag: str) -> None:
    with LoggedContainer(image_tag, test_name="logs-success", command="echo SUCCESS_LOG_LINE") as container:
        container.get_wrapped_container().wait(timeout=5)

    log = _logs_dir("logs-success") / "container.log"
    assert log.exists(), "container.log not written on success"
    assert b"SUCCESS_LOG_LINE" in log.read_bytes()


if __name__ == "__main__":
    pytest_bazel.main()
