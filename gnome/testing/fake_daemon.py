"""Control and lifetime of the GJS view-service fake used by extension tests."""

import re
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from gnome.testing.session import GnomeSession, exec_output, wait_until


@dataclass(frozen=True)
class FakeDaemon:
    session: GnomeSession
    destination: str
    object_path: str

    def call(self, method: str, *args: str) -> bytes:
        return self.session.dbus_call(self.destination, self.object_path, method, *args)

    def is_running(self) -> bool:
        return bool(
            self.session.exec(
                f"gdbus introspect --session --dest {shlex.quote(self.destination)} "
                f"--object-path {shlex.quote(self.object_path)}"
            ).exit_code
            == 0
        )

    def counters(self) -> tuple[int, int, int]:
        output = self.call("GetCounters")
        match = re.search(rb"\(uint32 (\d+), uint32 (\d+), uint32 (\d+)\)", output)
        if match is None:
            raise RuntimeError(f"fake daemon returned invalid counters: {output!r}")
        return int(match[1]), int(match[2]), int(match[3])


def _cleanup(daemon: FakeDaemon, pid: int) -> list[Exception]:
    errors: list[Exception] = []
    try:
        if daemon.is_running():
            daemon.call("Stop")
    except Exception as error:
        errors.append(error)
    try:
        # Also terminate a process that failed before exporting its control interface.
        result = daemon.session.exec(
            f"if kill -0 {pid} 2>/dev/null; then kill {pid} 2>/dev/null || ! kill -0 {pid} 2>/dev/null; fi"
        )
        if result.exit_code != 0:
            raise RuntimeError(f"fake daemon cleanup failed: {exec_output(result)}")
        wait_until(lambda: not daemon.is_running(), f"fake D-Bus service {daemon.destination} did not stop")
    except Exception as error:
        errors.append(error)
    try:
        result = daemon.session.exec("cat /tmp/fake-daemon.log")
        stdout, stderr = exec_output(result)
        if result.exit_code != 0:
            raise RuntimeError(f"fake daemon log capture failed: {(stdout + stderr).decode(errors='replace')}")
        (undeclared_outputs_dir() / "fake-daemon.log").write_bytes(stdout + stderr)
    except Exception as error:
        errors.append(error)
    return errors


@contextmanager
def fake_daemon(session: GnomeSession, *, destination: str, object_path: str) -> Iterator[FakeDaemon]:
    daemon = FakeDaemon(session, destination=destination, object_path=object_path)
    result = session.exec("gjs -m /fake-daemon.js >/tmp/fake-daemon.log 2>&1 & printf '%s' $!")
    stdout, stderr = exec_output(result)
    if result.exit_code != 0:
        raise RuntimeError(f"fake daemon could not start: {(stdout + stderr).decode(errors='replace')}")
    pid = int(stdout)
    failure: BaseException | None = None
    try:
        wait_until(daemon.is_running, f"fake D-Bus service {destination} did not start")
        yield daemon
    except BaseException as error:
        failure = error
        raise
    finally:
        errors = _cleanup(daemon, pid)
        if failure is not None:
            for cleanup_error in errors:
                failure.add_note(f"Fake daemon cleanup failed: {cleanup_error!r}")
        elif errors:
            raise ExceptionGroup("Fake daemon cleanup failed", errors)
