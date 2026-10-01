"""Headless GNOME startup and screenshot tests for the Plaid Spend extension.

The test boots the shipped extension ZIP in a GNOME Shell session on Xvfb. It
first checks the normal startup path with the desktop daemon absent, then loads
synthetic views through a test-only D-Bus interface and captures the panel and
popup. No Plaid credentials, accounts, or live service are used.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import docker.models.containers
import pytest
import pytest_bazel
from PIL import Image
from testcontainers.core.container import DockerContainer

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_review import retain_review_asset

_GNOME_SHELL_TEST = OciImage("_main/gnome/test_image/gnome_shell_test.rloc", "gnome-shell-test:pinned")
_EXTENSION_ZIP = "_main/finance/plaid/spend/gnome/plaid-spend-desktop.zip"
_EXTENSION_UUID = "plaid-spend@allegedly.works"
_FIXTURE_NAMES = ("ready_two_cards", "authentication_required", "offline")
_FIXTURE_DIR = "_main/finance/plaid/spend/gnome/fixtures"
_SCREEN_WIDTH = 1920
_EXTENSION_STATE_ENABLED = 1
_TEST_DBUS_DEST = "works.allegedly.PlaidSpendTest"
_TEST_DBUS_PATH = "/works/allegedly/PlaidSpendTest"


def _exec_output(result: docker.models.containers.ExecResult) -> tuple[bytes, bytes]:
    """Return demultiplexed Docker output as bytes."""
    stdout, stderr = cast(tuple[bytes | None, bytes | None], result.output)
    return stdout or b"", stderr or b""


def test_extension_metadata_supports_gnome_50() -> None:
    """Keep the distributed ZIP loadable by the GNOME Shell version on Rugged."""
    with zipfile.ZipFile(get_required_path(_EXTENSION_ZIP)) as extension_zip:
        metadata = json.loads(extension_zip.read("metadata.json"))

    assert "50" in metadata["shell-version"]


@pytest.fixture(scope="module")
def gnome_shell_test_image() -> str:
    return load_oci_image(_GNOME_SHELL_TEST)


@pytest.fixture(scope="module")
def extension_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("plaid-spend-extension")
    with zipfile.ZipFile(get_required_path(_EXTENSION_ZIP)) as extension_zip:
        extension_zip.extractall(dest)
    return dest


@pytest.fixture(scope="module")
def fixture_json_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("plaid-spend-fixtures")
    for fixture_name in _FIXTURE_NAMES:
        source = get_required_path(f"{_FIXTURE_DIR}/{fixture_name}.json")
        (dest / f"{fixture_name}.json").write_bytes(Path(source).read_bytes())
    return dest


@pytest.fixture(scope="module")
def render_session(
    gnome_shell_test_image: str, extension_dir: Path, fixture_json_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[docker.models.containers.Container, Path]]:
    """Share one Xvfb, D-Bus bus, and GNOME Shell process across all fixtures."""
    output_dir = tmp_path_factory.mktemp("plaid-spend-renders")
    output_dir.chmod(0o777)

    container = DockerContainer(gnome_shell_test_image)
    container.with_volume_mapping(str(extension_dir), f"/usr/share/gnome-shell/extensions/{_EXTENSION_UUID}", "ro")
    container.with_volume_mapping(str(fixture_json_dir), "/fixtures", "ro")
    container.with_volume_mapping(str(output_dir), "/out", "rw")

    with container:
        raw = container.get_wrapped_container()
        raw.exec_run(["/usr/local/bin/boot.sh"], detach=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if raw.exec_run(["test", "-f", "/tmp/boot.ready"]).exit_code == 0:
                break
            time.sleep(0.2)
        else:
            xvfb_log, _ = _exec_output(raw.exec_run(["cat", "/tmp/xvfb.log"], demux=True))
            pytest.fail(
                f"container boot.sh never produced /tmp/boot.ready within 30s\nxvfb.log:\n{xvfb_log.decode(errors='replace')}"
            )

        try:
            # Exercise the production D-Bus proxy path with no daemon present;
            # the gated test interface only supplies deterministic render data.
            _start_gnome_shell(raw)
            _wait_for_shell_bus(raw)
            _wait_for_extension_enabled(raw)
            _wait_for_test_dbus(raw)
            time.sleep(0.5)
            _assert_no_plaid_extension_error(raw)
        except (AssertionError, RuntimeError, TimeoutError) as error:
            _save_shell_log(raw, undeclared_outputs_dir() / "startup.shell.log")
            pytest.fail(f"render_session startup failed: {error}")

        yield raw, output_dir


def _exec_in_session(
    container: docker.models.containers.Container, shell_cmd: str, *, detach: bool = False
) -> docker.models.containers.ExecResult:
    """Run a command with the test container's persistent session bus and X display."""
    full_cmd = (
        "set -euo pipefail; "
        "source /tmp/dbus.env; "
        'export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"; '
        "export DISPLAY=:99; "
        f"{shell_cmd}"
    )
    return container.exec_run(["bash", "-c", full_cmd], demux=True, detach=detach)


def _start_gnome_shell(container: docker.models.containers.Container) -> None:
    command = (
        f"gsettings set org.gnome.shell enabled-extensions '[\"{_EXTENSION_UUID}\"]'; "
        "gsettings set org.gnome.shell disable-user-extensions false; "
        "export PLAID_SPEND_TEST=1; "
        "nohup gnome-shell --x11 >/tmp/shell.log 2>&1 &"
    )
    _exec_in_session(container, command, detach=True)


def _wait_for_shell_bus(container: docker.models.containers.Container, *, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = _exec_in_session(
            container,
            "gdbus introspect --session --dest org.gnome.Shell --object-path /org/gnome/Shell >/dev/null 2>&1",
        )
        if result.exit_code == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"gnome-shell never owned the bus name within {timeout_s}s")


def _wait_for_extension_enabled(container: docker.models.containers.Container, *, timeout_s: float = 10) -> None:
    deadline = time.monotonic() + timeout_s
    last_response = b""
    while time.monotonic() < deadline:
        result = _exec_in_session(
            container,
            "gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell "
            f"--method org.gnome.Shell.Extensions.GetExtensionInfo {shlex.quote(_EXTENSION_UUID)}",
        )
        stdout, stderr = _exec_output(result)
        last_response = stdout + stderr
        if result.exit_code == 0 and f"'state': <{_EXTENSION_STATE_ENABLED}.0>".encode() in last_response:
            return
        time.sleep(0.25)
    raise TimeoutError(
        f"extension {_EXTENSION_UUID} never reached ENABLED; last response: {last_response.decode(errors='replace')!r}"
    )


def _wait_for_test_dbus(container: docker.models.containers.Container, *, timeout_s: float = 10) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = _exec_in_session(
            container,
            f"gdbus introspect --session --dest {_TEST_DBUS_DEST} --object-path {_TEST_DBUS_PATH} >/dev/null 2>&1",
        )
        if result.exit_code == 0:
            return
        time.sleep(0.2)
    raise TimeoutError("Plaid Spend screenshot-test D-Bus interface was not exported")


def _test_dbus_call(
    container: docker.models.containers.Container, method: str, *args: str
) -> docker.models.containers.ExecResult:
    arg_str = " ".join(args)
    return _exec_in_session(
        container,
        f"gdbus call --session --dest {_TEST_DBUS_DEST} "
        f"--object-path {_TEST_DBUS_PATH} --method {_TEST_DBUS_DEST}.{method} {arg_str}",
    )


def _test_dbus_call_output(container: docker.models.containers.Container, method: str, *args: str) -> bytes:
    result = _test_dbus_call(container, method, *args)
    stdout, stderr = _exec_output(result)
    if result.exit_code != 0:
        raise RuntimeError(f"{method} failed: {stderr.decode(errors='replace')}")
    return stdout


def _reload_fixture(container: docker.models.containers.Container, fixture_path: str) -> None:
    _test_dbus_call_output(container, "Reload", shlex.quote(fixture_path))
    time.sleep(0.3)


def _panel_label(container: docker.models.containers.Container) -> str:
    output = _test_dbus_call_output(container, "GetPanelLabel").decode().strip()
    parsed = ast.literal_eval(output)
    if not isinstance(parsed, tuple) or len(parsed) != 1 or not isinstance(parsed[0], str):
        raise RuntimeError(f"GetPanelLabel returned an unexpected value: {output!r}")
    return parsed[0]


def _open_menu(container: docker.models.containers.Container) -> tuple[int, int, int, int]:
    _test_dbus_call_output(container, "OpenMenu")
    time.sleep(0.2)
    output = _test_dbus_call_output(container, "GetMenuGeometry")
    match = re.search(rb"\((-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\)", output)
    if not match:
        raise RuntimeError(f"GetMenuGeometry returned an unparseable value: {output!r}")
    geometry = (int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)))
    if geometry[2] <= 0 or geometry[3] <= 0:
        raise RuntimeError(f"menu has non-positive dimensions: {geometry}")
    return geometry


def _close_menu(container: docker.models.containers.Container) -> None:
    _test_dbus_call_output(container, "CloseMenu")


def _screenshot(container: docker.models.containers.Container, path: str) -> None:
    result = _exec_in_session(container, f"scrot --display :99 --overwrite {shlex.quote(path)}")
    if result.exit_code != 0:
        _, stderr = _exec_output(result)
        raise RuntimeError(f"scrot failed: {stderr.decode(errors='replace')}")


def _shell_log(container: docker.models.containers.Container) -> bytes:
    result = container.exec_run(["cat", "/tmp/shell.log"], demux=True)
    stdout, stderr = _exec_output(result)
    return stdout + stderr


def _assert_no_plaid_extension_error(container: docker.models.containers.Container) -> None:
    log = _shell_log(container).decode(errors="replace")
    extension_error = f"/extensions/{_EXTENSION_UUID}/extension.js:"
    if extension_error in log or f"Extension {_EXTENSION_UUID}:" in log:
        raise AssertionError(f"GNOME logged a Plaid Spend extension error during default startup:\n{log}")


def _save_shell_log(container: docker.models.containers.Container, path: Path) -> None:
    path.write_bytes(_shell_log(container))


def _crop_combined(full: Image.Image, menu_geometry: tuple[int, int, int, int]) -> Image.Image:
    menu_x, menu_y, menu_width, menu_height = menu_geometry
    left = max(0, menu_x)
    right = min(full.width, max(menu_x + menu_width, _SCREEN_WIDTH))
    bottom = min(full.height, menu_y + menu_height)
    if right <= left or bottom <= 0:
        raise AssertionError(f"menu lies outside the screenshot: {menu_geometry}, screen={full.size}")
    return full.crop((left, 0, right, bottom))


@pytest.mark.parametrize(
    ("fixture_name", "expected_label"),
    [("ready_two_cards", "$149.45 !"), ("authentication_required", "Sign in"), ("offline", "Offline")],
)
def test_render(
    render_session: tuple[docker.models.containers.Container, Path],
    tmp_path: Path,
    fixture_name: str,
    expected_label: str,
) -> None:
    container, output_dir = render_session
    image_name = f"{fixture_name}.png"
    fixture_path = f"/fixtures/{fixture_name}.json"
    output_path = f"/out/{image_name}"

    _close_menu(container)
    _reload_fixture(container, fixture_path)
    assert _panel_label(container) == expected_label
    menu_geometry = _open_menu(container)
    _screenshot(container, output_path)
    _close_menu(container)

    full_path = output_dir / image_name
    assert full_path.is_file(), f"scrot did not produce {full_path}"
    actual_path = tmp_path / f"{fixture_name}.cropped.png"
    _crop_combined(Image.open(full_path), menu_geometry).save(actual_path)
    retain_review_asset(
        actual_path, title="Plaid Spend GNOME extension", label=fixture_name.replace("_", " "), name=image_name
    )


if __name__ == "__main__":
    pytest_bazel.main()
