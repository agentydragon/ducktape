"""Headless GNOME session mechanics shared by extension render tests."""

import re
import shlex
import time
from collections.abc import Callable, Mapping
from io import BytesIO
from pathlib import Path
from typing import cast

from docker.models.containers import Container, ExecResult
from PIL import Image

from util.testing.output_writer import publish_output
from util.testing.undeclared_outputs import undeclared_outputs_dir


def exec_output(result: ExecResult) -> tuple[bytes, bytes]:
    stdout, stderr = cast(tuple[bytes | None, bytes | None], result.output)
    return stdout or b"", stderr or b""


def _wait(check: Callable[[], bool], *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        # Bounded polling interval, not a guess at how long the operation needs.
        time.sleep(0.2)


class GnomeSession:
    def __init__(self, container: Container, *, extension: str, destination: str, object_path: str) -> None:
        self.container = container
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
        _wait(
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
        _wait(
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

        _wait(enabled, timeout=10, description=f"extension {self.extension} never reached ENABLED")
        _wait(
            lambda: (
                self.exec(
                    f"gdbus introspect --session --dest {shlex.quote(self.destination)} --object-path {shlex.quote(self.object_path)}"
                ).exit_code
                == 0
            ),
            timeout=10,
            description=f"test D-Bus interface {self.destination} was not exported",
        )

    def call(self, method: str, *args: str) -> bytes:
        result = self.exec(
            f"gdbus call --session --timeout 10 --dest {shlex.quote(self.destination)} "
            f"--object-path {shlex.quote(self.object_path)} --method {shlex.quote(self.destination + '.' + method)} "
            + " ".join(shlex.quote(arg) for arg in args)
        )
        stdout, stderr = exec_output(result)
        if result.exit_code != 0:
            raise RuntimeError(f"{method} failed: {(stdout + stderr).decode(errors='replace')}")
        return stdout

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

    def screenshot(self, path: str) -> None:
        self.wait_for_paint()
        result = self.exec(f"scrot --display :99 --overwrite {shlex.quote(path)}")
        if result.exit_code != 0:
            raise RuntimeError(f"scrot failed: {exec_output(result)}")

    def log(self) -> bytes:
        stdout, stderr = exec_output(self.container.exec_run(["cat", "/tmp/shell.log"], demux=True))
        return stdout + stderr

    def save_log(self, path: Path) -> None:
        path.write_bytes(self.log())


def crop_panel_menu(full: Image.Image, geometry: tuple[int, int, int, int]) -> Image.Image:
    x, y, width, height = geometry
    left, bottom = max(0, x), min(full.height, y + height)
    if width <= 0 or height <= 0 or left >= full.width or bottom <= 0:
        raise AssertionError(f"menu lies outside the screenshot: {geometry}, screen={full.size}")
    return full.crop((left, 0, full.width, bottom))


def capture_panel_menu(
    session: GnomeSession,
    output_dir: Path,
    *,
    geometry: tuple[int, int, int, int],
    name: str,
    title: str,
    label: str,
    case_id: str,
) -> Path:
    """Capture and publish the panel/menu crop; output_dir is mounted at /out."""
    session.screenshot(f"/out/{name}")
    full_path = output_dir / name
    assert full_path.is_file(), f"scrot did not produce {full_path}"
    with Image.open(full_path) as full:
        cropped = crop_panel_menu(full, geometry)
        png = BytesIO()
        cropped.save(png, format="PNG")
    return publish_output(
        undeclared_outputs_dir(),
        case_id=case_id,
        checkpoint=None,
        role="screenshot",
        data=png.getvalue(),
        suffix="png",
        label=label,
        title=title,
    )
