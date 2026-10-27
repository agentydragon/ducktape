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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
import pytest_bazel
from testcontainers.core.container import DockerContainer

from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.gnome import GnomeSession, capture_panel_menu, exec_output
from util.testing.undeclared_outputs import undeclared_outputs_dir

_GNOME_SHELL_TEST = OciImage(
    "_main/gnome/test_image/gnome_shell_test.rloc",
    "gnome-shell-test:pinned",
    "_main/gnome/test_image/gnome_shell_test_ci_load.sh",
)
_EXTENSION_ZIP = "_main/finance/plaid/spend/gnome/plaid-spend-desktop.zip"
_EXTENSION_UUID = "plaid-spend@allegedly.works"
_FIXTURE_NAMES = (
    "ready_two_cards",
    "authentication_required",
    "offline",
    "retrying",
    "allowance_paced",
    "allowance_warming",
    "allowance_exhausted",
)
_FIXTURE_DIR = "_main/finance/plaid/spend/gnome/fixtures"
_TEST_DBUS_DEST = "works.allegedly.PlaidSpendTest"
_TEST_DBUS_PATH = "/works/allegedly/PlaidSpendTest"
_FAKE_DBUS_DEST = "works.allegedly.PlaidSpendFake"
_FAKE_DBUS_PATH = "/works/allegedly/PlaidSpendFake"
_FAKE_DBUS_SERVICE = "_main/finance/plaid/spend/gnome/fake-daemon.js"


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
) -> Iterator[tuple[GnomeSession, Path]]:
    """Share one Xvfb, D-Bus bus, and GNOME Shell process across all fixtures."""
    output_dir = tmp_path_factory.mktemp("plaid-spend-renders")
    output_dir.chmod(0o777)

    container = DockerContainer(gnome_shell_test_image)
    container.with_volume_mapping(str(extension_dir), f"/usr/share/gnome-shell/extensions/{_EXTENSION_UUID}", "ro")
    container.with_volume_mapping(str(fixture_json_dir), "/fixtures", "ro")
    container.with_volume_mapping(str(get_required_path(_FAKE_DBUS_SERVICE)), "/fake-daemon.js", "ro")
    container.with_volume_mapping(str(output_dir), "/out", "rw")

    with container:
        session = GnomeSession(
            container.get_wrapped_container(),
            extension=_EXTENSION_UUID,
            destination=_TEST_DBUS_DEST,
            object_path=_TEST_DBUS_PATH,
        )
        try:
            session.boot()
            session.start(environment={"PLAID_SPEND_TEST": "1", "PLAID_SPEND_TEST_NOW": "2026-10-15T12:00:00.000Z"})
            session.wait_for_paint()
            _assert_no_plaid_extension_error(session)
        except AssertionError, TimeoutError, RuntimeError:
            session.save_log(undeclared_outputs_dir() / "startup.shell.log")
            raise
        try:
            yield session, output_dir
        finally:
            try:
                session.save_log(undeclared_outputs_dir() / "session.shell.log")
            except Exception as error:
                (undeclared_outputs_dir() / "session-shell-log-capture-error.txt").write_text(
                    f"{type(error).__name__}: {error}\n"
                )


def _string_reply(reply: bytes) -> str:
    output = reply.decode().strip()
    parsed = ast.literal_eval(output)
    if not isinstance(parsed, tuple) or len(parsed) != 1 or not isinstance(parsed[0], str):
        raise RuntimeError(f"Unexpected D-Bus string reply: {output!r}")
    return parsed[0]


def _fake_daemon_call(session: GnomeSession, method: str, *args: str) -> bytes:
    result = session.exec(
        f"gdbus call --session --timeout 10 --dest {shlex.quote(_FAKE_DBUS_DEST)} "
        f"--object-path {shlex.quote(_FAKE_DBUS_PATH)} "
        f"--method {shlex.quote(_FAKE_DBUS_DEST + '.' + method)} " + " ".join(shlex.quote(arg) for arg in args)
    )
    stdout, stderr = exec_output(result)
    if result.exit_code != 0:
        raise RuntimeError(f"fake daemon {method} failed: {(stdout + stderr).decode(errors='replace')}")
    return stdout


def _fake_daemon_counters(session: GnomeSession) -> tuple[int, int, int]:
    output = _fake_daemon_call(session, "GetCounters")
    match = re.search(rb"\(uint32 (\d+), uint32 (\d+), uint32 (\d+)\)", output)
    if match is None:
        raise RuntimeError(f"fake daemon returned invalid counters: {output!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _wait_until(check: Callable[[], bool], description: str) -> None:
    deadline = time.monotonic() + 15
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.2)


def _card_view(amount_minor_units: int) -> str:
    return json.dumps(
        {
            "generated_at": "2026-10-18T12:00:00Z",
            "cards": [{"spend_minor_units": amount_minor_units, "currency": "USD"}],
        }
    )


def test_view_signal_wins_initial_get_and_owner_restart_rehydrates(render_session: tuple[GnomeSession, Path]) -> None:
    session, _ = render_session
    session.exec("gjs -m /fake-daemon.js >/tmp/plaid-spend-fake-daemon.log 2>&1 &")
    try:
        _wait_until(
            lambda: (
                session.exec(
                    f"gdbus introspect --session --dest {shlex.quote(_FAKE_DBUS_DEST)} "
                    f"--object-path {shlex.quote(_FAKE_DBUS_PATH)}"
                ).exit_code
                == 0
            ),
            "fake Plaid Spend D-Bus service did not start",
        )
        _wait_until(lambda: _fake_daemon_counters(session)[0] == 1, "extension did not request the initial view")

        _fake_daemon_call(session, "SetView", _card_view(2000))
        _wait_until(
            lambda: _string_reply(session.call("GetPanelLabel")) == "$20",
            "extension did not project the ViewChanged signal",
        )
        _fake_daemon_call(session, "ReleaseGetView")
        session.wait_for_paint()
        assert _fake_daemon_counters(session) == (1, 1, 0)
        assert _string_reply(session.call("GetPanelLabel")) == "$20"

        test_clock = datetime.fromisoformat("2026-10-15T12:00:00+00:00").timestamp()
        _fake_daemon_call(session, "SetTiming", str(test_clock - 75), str(test_clock + 20))
        _wait_until(
            lambda: all(
                text in _string_reply(session.call("GetMenuText"))
                for text in ("Last attempt 1m 15s ago", "Retry in 20s")
            ),
            "extension did not project desktop retry properties",
        )

        _fake_daemon_call(session, "SetViewSilently", _card_view(3000))
        _fake_daemon_call(session, "ReleaseName")
        _wait_until(
            lambda: _string_reply(session.call("GetDaemonOwner")) == "",
            "extension did not observe the daemon going away",
        )
        assert "Last attempt" not in _string_reply(session.call("GetMenuText"))
        _fake_daemon_call(session, "AcquireName")
        _wait_until(lambda: _fake_daemon_counters(session)[0] == 2, "extension did not rehydrate after daemon restart")
        _fake_daemon_call(session, "ReleaseGetView")
        _wait_until(
            lambda: _string_reply(session.call("GetPanelLabel")) == "$30",
            "extension did not adopt the restarted daemon's view",
        )
        assert _fake_daemon_counters(session) == (2, 2, 0)
    finally:
        session.exec(
            f"if gdbus introspect --session --dest {shlex.quote(_FAKE_DBUS_DEST)} "
            f"--object-path {shlex.quote(_FAKE_DBUS_PATH)} >/dev/null 2>&1; then "
            f"gdbus call --session --dest {shlex.quote(_FAKE_DBUS_DEST)} "
            f"--object-path {shlex.quote(_FAKE_DBUS_PATH)} "
            f"--method {shlex.quote(_FAKE_DBUS_DEST + '.Stop')} >/dev/null; fi"
        )


def _assert_no_plaid_extension_error(container: GnomeSession) -> None:
    log = container.log().decode(errors="replace")
    extension_error = f"/extensions/{_EXTENSION_UUID}/extension.js:"
    if extension_error in log or f"Extension {_EXTENSION_UUID}:" in log:
        raise AssertionError(f"GNOME logged a Plaid Spend extension error during default startup:\n{log}")


@contextmanager
def _menu(session: GnomeSession, fixture_name: str, expected_label: str) -> Iterator[tuple[int, int, int, int]]:
    session.close_menu()
    session.reload(f"/fixtures/{fixture_name}.json")
    assert _string_reply(session.call("GetPanelLabel")) == expected_label
    geometry = session.open_menu()
    try:
        yield geometry
    finally:
        session.close_menu()


def _allowance_menu_text(session: GnomeSession) -> str:
    menu_text = _string_reply(session.call("GetMenuText"))
    assert "Synthetic card" not in menu_text
    assert "Check a purchase / dashboard" in menu_text
    return menu_text


def _capture(session: GnomeSession, output_dir: Path, geometry: tuple[int, int, int, int], name: str) -> None:
    capture_panel_menu(
        session,
        output_dir,
        geometry=geometry,
        name=f"{name}.png",
        title="Plaid Spend GNOME extension",
        label=name.replace("_", " "),
    )


@pytest.mark.parametrize(
    ("fixture_name", "expected_label"),
    [("ready_two_cards", "$149 !"), ("authentication_required", "Sign in"), ("offline", "Offline")],
)
def test_card_status(render_session: tuple[GnomeSession, Path], fixture_name: str, expected_label: str) -> None:
    session, output_dir = render_session
    with _menu(session, fixture_name, expected_label) as geometry:
        _capture(session, output_dir, geometry, fixture_name)


def test_allowance_paced(render_session: tuple[GnomeSession, Path]) -> None:
    session, output_dir = render_session
    with _menu(session, "allowance_paced", "Flex $200 !") as geometry:
        menu_text = _allowance_menu_text(session)
        assert "7d $28/day" in menu_text
        assert "30d $18/day" in menu_text
        assert "Leash ~$23/day" in menu_text
        assert "7d unmatched 3 ($12)" in menu_text
        assert "Sync 1h ago" in menu_text
        assert "View updated" not in menu_text
        assert "sustainability target" not in menu_text
        assert "past pace is not opening debt" not in menu_text
        _capture(session, output_dir, geometry, "allowance_paced")


def test_allowance_warming(render_session: tuple[GnomeSession, Path]) -> None:
    session, output_dir = render_session
    with _menu(session, "allowance_warming", "Flex $700") as geometry:
        menu_text = _allowance_menu_text(session)
        assert "Pace warming up" in menu_text
        assert "7d n/a" in menu_text
        assert "Sync 59m ago" in menu_text
        _capture(session, output_dir, geometry, "allowance_warming")


def test_allowance_exhausted(render_session: tuple[GnomeSession, Path]) -> None:
    session, output_dir = render_session
    with _menu(session, "allowance_exhausted", "Flex -<$1 !!") as geometry:
        assert "Exhausted" in _allowance_menu_text(session)
        _capture(session, output_dir, geometry, "allowance_exhausted")


def test_desktop_retry_timing_ticks_and_stays_separate_from_server_sync(
    render_session: tuple[GnomeSession, Path],
) -> None:
    session, output_dir = render_session
    with _menu(session, "retrying", "Flex unavailable") as geometry:
        menu_text = _string_reply(session.call("GetMenuText"))
        assert "Last attempt 1m 15s ago" in menu_text
        assert "Retry in 20s" in menu_text
        assert "Sync 1h ago" in menu_text
        session.call("AdvanceClock", "5")
        menu_text = _string_reply(session.call("GetMenuText"))
        assert "Last attempt 1m 20s ago" in menu_text
        assert "Retry in 15s" in menu_text
        assert "Sync 1h ago" in menu_text
        _capture(session, output_dir, geometry, "retrying")


def test_authentication_required_has_no_retry_countdown(render_session: tuple[GnomeSession, Path]) -> None:
    session, _ = render_session
    with _menu(session, "authentication_required", "Sign in"):
        menu_text = _string_reply(session.call("GetMenuText"))
        assert "Last attempt 1m 15s ago" in menu_text
        assert "Sign-in required" in menu_text
        assert "Retry in" not in menu_text


if __name__ == "__main__":
    pytest_bazel.main()
