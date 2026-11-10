"""Headless GNOME session mechanics shared by extension render tests."""

import ast
import re
import shlex
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast

from docker.models.containers import Container, ExecResult
from PIL import Image
from python.runfiles import Runfiles
from testcontainers.core.container import DockerContainer

from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def exec_output(result: ExecResult) -> tuple[bytes, bytes]:
    stdout, stderr = cast(tuple[bytes | None, bytes | None], result.output)
    return stdout or b"", stderr or b""


def wait_until(check: Callable[[], bool], description: str, *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        # Bounded polling interval, not a guess at how long the operation needs.
        time.sleep(0.2)


class GnomeSession:
    def __init__(
        self, container: Container, *, screenshot_dir: Path, extension: str, destination: str, object_path: str
    ) -> None:
        self.container = container
        self.screenshot_dir = screenshot_dir
        self.extension = extension
        self.destination = destination
        self.object_path = object_path

    def exec(self, command: str, *, detach: bool = False) -> ExecResult:
        return self.container.exec_run(
            [
                "bash",
                "-c",
                'set -euo pipefail; source /tmp/dbus.env; export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"; export DISPLAY=:99; '
                + command,
            ],
            demux=True,
            detach=detach,
        )

    def boot(self) -> None:
        self.container.exec_run(["/usr/local/bin/boot.sh"], detach=True)
        wait_until(
            lambda: self.container.exec_run(["test", "-f", "/tmp/boot.ready"]).exit_code == 0,
            timeout=30,
            description="container boot.sh never produced /tmp/boot.ready",
        )

    def start(self, *, environment: Mapping[str, str]) -> None:
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in environment.items())
        self.exec(
            "gsettings set org.gnome.shell disable-user-extensions false; "
            f"gsettings set org.gnome.shell enabled-extensions {shlex.quote(str([self.extension]))}; "
            f"{('export ' + exports + '; ') if exports else ''}nohup gnome-shell --x11 >/tmp/shell.log 2>&1 &",
            detach=True,
        )
        wait_until(
            lambda: (
                self.exec("gdbus introspect --session --dest org.gnome.Shell --object-path /org/gnome/Shell").exit_code
                == 0
            ),
            timeout=60,
            description="gnome-shell never owned its bus name",
        )

        def enabled() -> bool:
            result = self.exec(
                "gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell "
                f"--method org.gnome.Shell.Extensions.GetExtensionInfo {shlex.quote(self.extension)}"
            )
            stdout, _ = exec_output(result)
            return result.exit_code == 0 and b"'state': <1.0>" in stdout

        wait_until(enabled, timeout=10, description=f"extension {self.extension} never reached ENABLED")
        wait_until(
            lambda: (
                self.exec(
                    f"gdbus introspect --session --dest {shlex.quote(self.destination)} --object-path {shlex.quote(self.object_path)}"
                ).exit_code
                == 0
            ),
            timeout=10,
            description=f"test D-Bus interface {self.destination} was not exported",
        )

    def dbus_call(self, destination: str, object_path: str, method: str, *args: str) -> bytes:
        # gdbus replaces non-ASCII text with '?' in the container's default C locale.
        result = self.exec(
            f"LC_ALL=C.UTF-8 gdbus call --session --timeout 10 --dest {shlex.quote(destination)} "
            f"--object-path {shlex.quote(object_path)} --method {shlex.quote(destination + '.' + method)} "
            + " ".join(shlex.quote(arg) for arg in args)
        )
        stdout, stderr = exec_output(result)
        if result.exit_code != 0:
            raise RuntimeError(f"{destination}.{method} failed: {(stdout + stderr).decode(errors='replace')}")
        return stdout

    def call(self, method: str, *args: str) -> bytes:
        return self.dbus_call(self.destination, self.object_path, method, *args)

    def reload(self, fixture_path: str) -> None:
        self.call("Reload", fixture_path)
        self.wait_for_paint()

    def wait_for_paint(self) -> None:
        self.call("WaitForPaint")

    def open_menu(self) -> tuple[int, int, int, int]:
        self.call("OpenMenu")
        self.wait_for_paint()
        output = self.call("GetMenuGeometry")
        match = re.search(rb"\((-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\)", output)
        if match is None:
            raise RuntimeError(f"GetMenuGeometry returned unparseable output: {output!r}")
        geometry = (int(match[1]), int(match[2]), int(match[3]), int(match[4]))
        if geometry[2] <= 0 or geometry[3] <= 0:
            raise AssertionError(f"menu has non-positive dimensions after paint: {geometry}")
        return geometry

    def close_menu(self) -> None:
        self.call("CloseMenu")

    def screenshot(self) -> bytes:
        self.wait_for_paint()
        with tempfile.NamedTemporaryFile(dir=self.screenshot_dir, suffix=".png") as output:
            path = Path(output.name)
            path.chmod(0o666)
            result = self.exec(f"scrot --display :99 --overwrite {shlex.quote(f'/out/{path.name}')}")
            if result.exit_code != 0:
                raise RuntimeError(f"scrot failed: {exec_output(result)}")
            return path.read_bytes()

    def log(self) -> bytes:
        stdout, stderr = exec_output(self.container.exec_run(["cat", "/tmp/shell.log"], demux=True))
        return stdout + stderr

    def save_log(self, path: Path) -> None:
        path.write_bytes(self.log())


@contextmanager
def gnome_session(
    image: str,
    *,
    extension_dir: Path,
    extension: str,
    fixture_dir: Path,
    fake_daemon_script: Path,
    screenshot_dir: Path,
    destination: str,
    object_path: str,
    environment: Mapping[str, str],
    display_size: tuple[int, int] = (1920, 500),
    compile_extension_schemas: bool = False,
    activate_display: bool = False,
) -> Iterator[GnomeSession]:
    screenshot_dir.chmod(0o777)  # GNOME writes screenshots as a different uid.
    container = DockerContainer(image)
    container.with_env("RENDER_WIDTH", str(display_size[0]))
    container.with_env("RENDER_HEIGHT", str(display_size[1]))
    container.with_volume_mapping(
        str(extension_dir),
        f"/usr/share/gnome-shell/extensions/{extension}",
        "rw" if compile_extension_schemas else "ro",
    )
    container.with_volume_mapping(str(fixture_dir), "/fixtures", "ro")
    container.with_volume_mapping(str(fake_daemon_script), "/fake-daemon.js", "ro")
    container.with_volume_mapping(
        str(Runfiles.CreateOrRaise().root() / "ducktape/gnome/testing/fake-daemon.js"), "/gnome-fake-daemon.js", "ro"
    )
    container.with_volume_mapping(str(screenshot_dir), "/out", "rw")
    with container:
        session = GnomeSession(
            container.get_wrapped_container(),
            screenshot_dir=screenshot_dir,
            extension=extension,
            destination=destination,
            object_path=object_path,
        )
        failure: BaseException | None = None
        try:
            session.boot()
            if activate_display:
                result = session.exec("dbus-update-activation-environment DISPLAY DBUS_SESSION_BUS_ADDRESS")
                if result.exit_code != 0:
                    raise RuntimeError(f"Could not set D-Bus activation environment: {exec_output(result)}")
            if compile_extension_schemas:
                result = session.container.exec_run(
                    ["glib-compile-schemas", f"/usr/share/gnome-shell/extensions/{extension}/schemas"], demux=True
                )
                if result.exit_code != 0:
                    raise RuntimeError(f"Extension schema compilation failed: {exec_output(result)}")
            session.start(environment=environment)
            session.wait_for_paint()
            yield session
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                session.save_log(undeclared_outputs_dir() / "session.shell.log")
            except Exception as log_error:
                if failure is None:
                    raise
                failure.add_note(f"GNOME session log capture failed: {log_error!r}")


def string_reply(reply: bytes) -> str:
    output = reply.decode().strip()
    parsed = ast.literal_eval(output)
    if not isinstance(parsed, tuple) or len(parsed) != 1 or not isinstance(parsed[0], str):
        raise RuntimeError(f"Unexpected D-Bus string reply: {output!r}")
    return parsed[0]


def crop_panel_menu(full: Image.Image, geometry: tuple[int, int, int, int], *, left_padding: int = 0) -> Image.Image:
    x, y, width, height = geometry
    left, bottom = max(0, x - left_padding), min(full.height, y + height)
    if width <= 0 or height <= 0 or left >= full.width or bottom <= 0:
        raise AssertionError(f"menu lies outside the screenshot: {geometry}, screen={full.size}")
    return full.crop((left, 0, full.width, bottom))


@dataclass(frozen=True)
class GnomeMenuCapture:
    """Publish crops from a shared GNOME session under one test's output identity."""

    session: GnomeSession
    outputs: BoundTestOutputs
    title: str

    def capture(
        self, geometry: tuple[int, int, int, int], *, label: str, checkpoint: str | None = None, left_padding: int = 0
    ) -> Path:
        with Image.open(BytesIO(self.session.screenshot())) as full:
            cropped = crop_panel_menu(full, geometry, left_padding=left_padding)
            png = BytesIO()
            cropped.save(png, format="PNG")
        return self.outputs.publish(
            checkpoint=checkpoint, role="screenshot", data=png.getvalue(), suffix="png", label=label, title=self.title
        )
