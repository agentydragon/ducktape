"""Control and lifetime of the GJS view-service fake used by extension tests."""

import re
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from util.testing.gnome import GnomeSession, exec_output, wait_until
from util.testing.undeclared_outputs import undeclared_outputs_dir


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


@contextmanager
def fake_daemon(session: GnomeSession, *, destination: str, object_path: str) -> Iterator[FakeDaemon]:
    daemon = FakeDaemon(session, destination=destination, object_path=object_path)
    result = session.exec("gjs -m /fake-daemon.js >/tmp/fake-daemon.log 2>&1 & printf '%s' $!")
    stdout, stderr = exec_output(result)
    if result.exit_code != 0:
        raise RuntimeError(f"fake daemon could not start: {(stdout + stderr).decode(errors='replace')}")
    pid = int(stdout)
    try:
        wait_until(daemon.is_running, f"fake D-Bus service {destination} did not start")
        yield daemon
    finally:
        try:
            if daemon.is_running():
                daemon.call("Stop")
        finally:
            # Also terminate a process that failed before exporting its control interface.
            result = session.exec(
                f"if kill -0 {pid} 2>/dev/null; then kill {pid} 2>/dev/null || ! kill -0 {pid} 2>/dev/null; fi"
            )
            if result.exit_code != 0:
                raise RuntimeError(f"fake daemon cleanup failed: {exec_output(result)}")
            wait_until(lambda: not daemon.is_running(), f"fake D-Bus service {destination} did not stop")
            stdout, stderr = exec_output(session.exec("cat /tmp/fake-daemon.log"))
            (undeclared_outputs_dir() / "fake-daemon.log").write_bytes(stdout + stderr)
