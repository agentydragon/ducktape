"""Tests for LoggedContainer: log persistence, exec, volumes, put_archive."""

import io
import tarfile
from pathlib import Path

import pytest
import pytest_bazel

from third_party.containers.rlocations import DEBIAN_SLIM
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer, LoggedContainerFactory
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="module")
def image_tag() -> str:
    """Load debian-slim and return its tag."""
    load_oci_image(DEBIAN_SLIM)
    return DEBIAN_SLIM.tag


@pytest.fixture
def logged_container(request: pytest.FixtureRequest) -> LoggedContainerFactory:
    """Factory fixture: LoggedContainer with test_name auto-derived."""
    test_name = request.node.name

    def _create(*args, **kwargs) -> LoggedContainer:
        return LoggedContainer(*args, test_name=test_name, **kwargs)

    return _create


def _logs_dir(test_name: str) -> Path:
    return undeclared_outputs_dir() / test_name


# --- Log persistence ---


def test_logs_collected_on_success(image_tag: str) -> None:
    with LoggedContainer(image_tag, test_name="logs-success", command="echo SUCCESS_LOG_LINE") as container:
        container.get_wrapped_container().wait(timeout=5)

    log = _logs_dir("logs-success") / "container.log"
    assert log.exists(), "container.log not written on success"
    assert b"SUCCESS_LOG_LINE" in log.read_bytes()


def test_logs_collected_on_exec_failure(image_tag: str) -> None:
    def _run() -> None:
        with LoggedContainer(image_tag, test_name="logs-failure", command="sleep infinity") as container:
            container.exec("echo FAILURE_LOG_LINE")
            raise AssertionError("deliberate failure")

    with pytest.raises(AssertionError):
        _run()

    log = _logs_dir("logs-failure") / "container.log"
    assert log.exists(), "container.log not written on failure"


def test_logs_collected_on_container_error(image_tag: str) -> None:
    with LoggedContainer(image_tag, test_name="logs-error", command="bash -c 'echo ERROR_LINE && exit 1'") as container:
        container.get_wrapped_container().wait(timeout=5)

    log = _logs_dir("logs-error") / "container.log"
    assert log.exists(), "container.log not written on container error"


# --- Container primitives ---


def test_exec(image_tag: str, logged_container: LoggedContainerFactory) -> None:
    with logged_container(image_tag, command="sleep infinity") as container:
        result = container.exec("echo EXEC_OK")
        assert result.exit_code == 0
        assert b"EXEC_OK" in result.output


def test_volume_mount_read(image_tag: str, logged_container: LoggedContainerFactory, tmp_path: Path) -> None:
    test_file = tmp_path / "input.txt"
    test_file.write_text("MOUNT_READ_OK")
    with logged_container(
        image_tag, command="sleep infinity", volumes=[(str(test_file), "/work/input.txt", "ro")]
    ) as container:
        result = container.exec("cat /work/input.txt")
        assert result.exit_code == 0
        assert b"MOUNT_READ_OK" in result.output


def test_volume_mount_write(image_tag: str, logged_container: LoggedContainerFactory, tmp_path: Path) -> None:
    with logged_container(image_tag, command="sleep infinity", volumes=[(str(tmp_path), "/output", "rw")]) as container:
        result = container.exec('bash -c "echo MOUNT_WRITE_OK > /output/test.txt"')
        assert result.exit_code == 0
    assert (tmp_path / "test.txt").read_text().strip() == "MOUNT_WRITE_OK"


def test_put_archive_and_cat(image_tag: str, logged_container: LoggedContainerFactory) -> None:
    with logged_container(image_tag, command="sleep infinity") as container:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            data = b"PUT_ARCHIVE_OK"
            info = tarfile.TarInfo(name="test.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        container.exec("mkdir -p /work")
        container.get_wrapped_container().put_archive("/work", buf.read())

        result = container.exec("cat /work/test.txt")
        assert result.exit_code == 0
        assert b"PUT_ARCHIVE_OK" in result.output


if __name__ == "__main__":
    pytest_bazel.main()
