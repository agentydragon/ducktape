"""Tests for LoggedContainer log persistence."""

from pathlib import Path

import pytest
import pytest_bazel

from third_party.containers import debian_slim
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="module")
def image_tag() -> str:
    """Load debian-slim and return its tag."""
    load_oci_image(debian_slim.IMAGE)
    return debian_slim.IMAGE.tag


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
