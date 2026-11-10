"""Failure reporting with Docker execution substituted at the external boundary."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
import pytest_bazel
from docker.models.containers import Container, ExecResult

from gnome.testing import session
from gnome.testing.fake_daemon import fake_daemon
from gnome.testing.session import GnomeSession


def _exec_result(exit_code: int, stdout: bytes = b"", stderr: bytes = b"") -> ExecResult:
    # Docker's stubs omit the stdout/stderr tuple returned with demux=True.
    return ExecResult(exit_code, cast(bytes, (stdout, stderr)))


@pytest.mark.parametrize("body_fails", [False, True])
def test_daemon_attempts_log_capture_after_cleanup_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body_fails: bool
) -> None:
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(tmp_path))
    session = GnomeSession(
        Container(),
        screenshot_dir=tmp_path,
        extension="test@example.invalid",
        destination="test.Extension",
        object_path="/test",
    )

    def execute(command: str, *, detach: bool = False) -> ExecResult:
        if command.startswith("gjs"):
            return _exec_result(0, stdout=b"123")
        if command.startswith("gdbus introspect"):
            return _exec_result(0)
        if command.startswith("gdbus call"):
            return _exec_result(1, stderr=b"Stop failed")
        if command.startswith("if kill"):
            raise RuntimeError("termination failed")
        if command == "cat /tmp/fake-daemon.log":
            return _exec_result(0, stdout=b"daemon diagnostics")
        raise AssertionError(command)

    monkeypatch.setattr(session, "exec", execute)
    body_error = RuntimeError("test body failed")

    def run_daemon() -> None:
        with fake_daemon(session, destination="test.Fake", object_path="/test"):
            if body_fails:
                raise body_error

    with pytest.raises(RuntimeError if body_fails else ExceptionGroup) as raised:
        run_daemon()

    assert (tmp_path / "fake-daemon.log").read_bytes() == b"daemon diagnostics"
    if body_fails:
        assert raised.value is body_error
        assert len(body_error.__notes__) == 2
    else:
        assert isinstance(raised.value, ExceptionGroup)
        assert len(raised.value.exceptions) == 2


@pytest.mark.parametrize("startup_fails", [False, True])
def test_session_log_error_preserves_startup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, startup_fails: bool
) -> None:
    container = MagicMock()
    container.__enter__.return_value = container
    container.get_wrapped_container.return_value = Container()
    monkeypatch.setattr(session, "DockerContainer", lambda image: container)
    monkeypatch.setattr(GnomeSession, "boot", lambda session: None)
    monkeypatch.setattr(GnomeSession, "wait_for_paint", lambda session: None)
    startup_error = RuntimeError("GNOME startup failed")

    def start(session: GnomeSession, *, environment: dict[str, str]) -> None:
        if startup_fails:
            raise startup_error

    def save_log(session: GnomeSession, path: Path) -> None:
        raise OSError("log unavailable")

    monkeypatch.setattr(GnomeSession, "start", start)
    monkeypatch.setattr(GnomeSession, "save_log", save_log)
    with (
        pytest.raises(RuntimeError if startup_fails else OSError) as raised,
        session.gnome_session(
            "test-image",
            extension_dir=tmp_path,
            extension="test@example.invalid",
            fixture_dir=tmp_path,
            fake_daemon_script=tmp_path / "fake.js",
            screenshot_dir=tmp_path,
            destination="test.Extension",
            object_path="/test",
            environment={},
        ),
    ):
        pass
    if startup_fails:
        assert raised.value is startup_error
        assert len(startup_error.__notes__) == 1


@pytest.mark.parametrize("body_fails", [False, True])
def test_session_always_saves_shell_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body_fails: bool) -> None:
    monkeypatch.setenv("TEST_UNDECLARED_OUTPUTS_DIR", str(tmp_path))
    container = MagicMock()
    container.__enter__.return_value = container
    container.get_wrapped_container.return_value.exec_run.return_value = _exec_result(0, stdout=b"shell diagnostics")
    monkeypatch.setattr(session, "DockerContainer", lambda image: container)
    monkeypatch.setattr(GnomeSession, "boot", lambda session: None)
    monkeypatch.setattr(GnomeSession, "start", lambda session, *, environment: None)
    monkeypatch.setattr(GnomeSession, "wait_for_paint", lambda session: None)

    def run_session() -> None:
        with session.gnome_session(
            "test-image",
            extension_dir=tmp_path,
            extension="test@example.invalid",
            fixture_dir=tmp_path,
            fake_daemon_script=tmp_path / "fake.js",
            screenshot_dir=tmp_path,
            destination="test.Extension",
            object_path="/test",
            environment={},
        ):
            if body_fails:
                raise AssertionError("test body failed")

    if body_fails:
        with pytest.raises(AssertionError, match="test body failed"):
            run_session()
    else:
        run_session()

    assert (tmp_path / "session.shell.log").read_bytes() == b"shell diagnostics"


if __name__ == "__main__":
    pytest_bazel.main()
