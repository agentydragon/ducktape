"""Render-health + PR-visuals publication tests for the ai-quota GNOME extension.

A single test container (//gnome/test_image:gnome_shell_test_image)
is started once per module: boot.sh inside it brings up Xvfb, the
postinst-equivalent caches, and a long-lived dbus session bus, then
blocks. A single gnome-shell is started inside that container (also
once per module) and the extension exports a session-bus interface
(works.allegedly.AiQuotaTest, gated on AI_QUOTA_FIXTURE) that lets this driver
swap fixture state, select connection state, open/close the popup, and query
the menu's screen geometry.

Each fixture renders to a single PNG capturing both the panel
indicator (with menu open, so the button shows its active state) and
the open popup menu below it. The crop spans from the menu's left edge
to the right edge of the screen and from the top of the panel to the
bottom of the menu — so reviewers see the indicator's icons / pace
labels and the menu's headers / bars / forecast strings in one image.
The fixtures themselves document the scenario each render covers.

There is no checked-in pixel golden — the renders publish to the PR's
visual-review page via the `visual-review.json` manifest (see
devinfra/pr_visuals/plans/goldens_to_pr_visuals.md); the hard gate is
that gnome-shell boots, the extension reaches ENABLED, and every
fixture opens its menu and screenshots successfully.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import shlex
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import pytest_bazel
from testcontainers.core.container import DockerContainer

from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_view_fixture
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.gnome import GnomeSession, capture_panel_menu, exec_output
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)


_GNOME_SHELL_TEST = OciImage("_main/gnome/test_image/gnome_shell_test.rloc", "gnome-shell-test:pinned")
_EXTENSION_ZIP = "_main/aiquota/gnome/aiquota.zip"
_EXTENSION_UUID = "aiquota@allegedly.works"

# Xvfb dims must match boot.sh — the combined panel+menu crop extends to
# the right edge, so the test driver needs to know it.

# gnome-shell ExtensionState (see js/misc/extensionUtils.js).

_TEST_DBUS_DEST = "works.allegedly.AiQuotaTest"
_TEST_DBUS_PATH = "/works/allegedly/AiQuotaTest"
_FAKE_DBUS_DEST = "works.allegedly.AiQuotaFake"
_FAKE_DBUS_PATH = "/works/allegedly/AiQuotaFake"
_FAKE_DBUS_SERVICE = "_main/aiquota/gnome/fake-daemon.js"


def test_extension_metadata_supports_gnome_50() -> None:
    """Keep the distributed zip loadable by wyrm2's GNOME Shell generation."""
    with zipfile.ZipFile(get_required_path(_EXTENSION_ZIP)) as extension_zip:
        metadata = json.loads(extension_zip.read("metadata.json"))

    assert "50" in metadata["shell-version"]


@pytest.fixture(scope="module")
def gnome_shell_test_image() -> str:
    return load_oci_image(_GNOME_SHELL_TEST)


@pytest.fixture(scope="module")
def extension_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Unzip the distribution zip into a single dir for bind-mounting."""
    dest = tmp_path_factory.mktemp("claude-quota-ext")
    with zipfile.ZipFile(get_required_path(_EXTENSION_ZIP)) as z:
        z.extractall(dest)
    return dest


@pytest.fixture(scope="module")
def fixture_json_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the normal API view payload for the GNOME JS test hook."""
    dest = tmp_path_factory.mktemp("ai-quota-fixtures")
    for fixture_name in FIXTURE_NAMES:
        src = get_required_path(f"_main/aiquota/testing/fixtures/{fixture_name}.yaml")
        data = load_quota_view_fixture(src)
        (dest / f"{fixture_name}.json").write_text(json.dumps(data, indent=2) + "\n")
    return dest


@pytest.fixture(scope="module")
def render_session(
    gnome_shell_test_image: str, extension_dir: Path, fixture_json_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[GnomeSession, Path]]:
    """Long-lived container + gnome-shell shared across the whole matrix.

    boot.sh starts Xvfb + a long-lived dbus session bus inside the
    container; we then launch gnome-shell once with one of the fixtures
    set as initial state (so the extension's test DBus interface is
    exported), wait for it to reach ENABLED, and yield the container
    handle + host-side output directory. Each parametrized test calls
    Reload + screenshots, leaving the shell process alone.
    """
    out_dir = tmp_path_factory.mktemp("ai-quota-renders")
    out_dir.chmod(0o777)  # gnome-shell writes the screenshot as a different uid

    container = DockerContainer(gnome_shell_test_image)
    container.with_volume_mapping(str(extension_dir), f"/usr/share/gnome-shell/extensions/{_EXTENSION_UUID}", "ro")
    container.with_volume_mapping(str(fixture_json_dir), "/fixtures", "ro")
    container.with_volume_mapping(str(get_required_path(_FAKE_DBUS_SERVICE)), "/fake-daemon.js", "ro")
    container.with_volume_mapping(str(out_dir), "/out", "rw")

    with container:
        session = GnomeSession(
            container.get_wrapped_container(),
            extension=_EXTENSION_UUID,
            destination=_TEST_DBUS_DEST,
            object_path=_TEST_DBUS_PATH,
        )
        try:
            session.boot()
            session.start(environment={"AI_QUOTA_FIXTURE": f"/fixtures/{FIXTURE_NAMES[0]}.json"})
            session.wait_for_paint()
        except AssertionError, TimeoutError, RuntimeError:
            session.save_log(undeclared_outputs_dir() / "startup.shell.log")
            raise
        yield session, out_dir


@pytest.fixture
def undeclared_dir() -> Path:
    out = undeclared_outputs_dir()
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_render(render_session: tuple[GnomeSession, Path], undeclared_dir: Path, fixture_name: str) -> None:
    session, output_dir = render_session
    try:
        session.close_menu()
        session.reload(f"/fixtures/{fixture_name}.json")
        if fixture_name == "claude_prepaid_balance":
            prepaid_text = _string_reply(session.call("GetPrepaidText", "claude"))
            assert prepaid_text.startswith("Claude prepaid balance")
            assert prepaid_text.endswith("$123.45")
        elif fixture_name == "claude_prepaid_balance_unavailable":
            assert _string_reply(session.call("GetPrepaidText", "claude")) == "Claude prepaid balance unavailable"
        elif fixture_name == "wire_fields":
            rows = json.loads(_string_reply(session.call("GetWindowRows", "codex")))
            assert len(rows) == 1
            assert rows[0].startswith("visible (5h):")
            assert "↻45m" in rows[0]
            assert "internal-only" not in "\n".join(rows)
        geometry = session.open_menu()
        try:
            capture_panel_menu(
                session,
                output_dir,
                geometry=geometry,
                name=f"{fixture_name}.png",
                title="AI quota GNOME extension",
                label=fixture_name.replace("_", " "),
            )
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / f"{fixture_name}.shell.log")
        pytest.fail(f"{fixture_name}: {e}")


def test_render_disconnected_stream(render_session: tuple[GnomeSession, Path], undeclared_dir: Path) -> None:
    session, output_dir = render_session
    try:
        session.close_menu()
        session.reload("/fixtures/hot.json")
        session.call("SetStreamStatus", "disconnected", "Connection to quota API was lost")
        session.call("SetSyncState", "958", "1019", "1000")
        session.wait_for_paint()
        stream_status = _string_reply(session.call("GetStreamStatus")).lower()
        assert "desktop sync attempt 42s ago" in stream_status
        assert "retry in 19s" in stream_status
        session.call("SetSyncState", "958", "1019", "1001")
        stream_status = _string_reply(session.call("GetStreamStatus")).lower()
        assert "desktop sync attempt 43s ago" in stream_status
        assert "retry in 18s" in stream_status
        session.call("SetSyncState", "958", "1019", "1000")
        session.wait_for_paint()
        geometry = session.open_menu()
        try:
            capture_panel_menu(
                session,
                output_dir,
                geometry=geometry,
                name="stream_disconnected.png",
                title="AI quota GNOME extension",
                label="stream disconnected",
            )
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / "stream_disconnected.shell.log")
        pytest.fail(f"stream disconnected: {e}")


def test_render_disconnected_without_snapshot(render_session: tuple[GnomeSession, Path], undeclared_dir: Path) -> None:
    session, output_dir = render_session
    try:
        session.close_menu()
        session.reload(f"/fixtures/{FIXTURE_NAMES[0]}.json")
        empty_view = json.dumps({"fetched_at": "2026-10-18T12:00:00Z", "providers": []})
        session.call("SetViewForTest", empty_view)
        session.call("SetStreamStatus", "disconnected", "AIQuota daemon is not running")
        session.wait_for_paint()
        geometry = session.open_menu()
        try:
            capture_panel_menu(
                session,
                output_dir,
                geometry=geometry,
                name="stream_disconnected_without_snapshot.png",
                title="AI quota GNOME extension",
                label="stream disconnected without snapshot",
            )
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / "stream_disconnected_without_snapshot.shell.log")
        pytest.fail(f"stream disconnected without snapshot: {e}")


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


def _view_json(revision: int) -> str:
    return json.dumps({"fetched_at": f"2026-10-18T12:00:0{revision}Z", "providers": []})


def test_view_signal_wins_getter_and_owner_restart_invalidates_reply(render_session: tuple[GnomeSession, Path]) -> None:
    session, _ = render_session
    session.exec("gjs -m /fake-daemon.js >/tmp/aiquota-fake-daemon.log 2>&1 &")
    try:
        _wait_until(
            lambda: (
                session.exec(
                    f"gdbus introspect --session --dest {shlex.quote(_FAKE_DBUS_DEST)} "
                    f"--object-path {shlex.quote(_FAKE_DBUS_PATH)}"
                ).exit_code
                == 0
            ),
            "fake AIQuota D-Bus service did not start",
        )
        session.call("ConnectDaemon")
        _wait_until(lambda: _fake_daemon_counters(session)[0] == 1, "extension did not request initial view")

        signal_view = _view_json(2)
        _fake_daemon_call(session, "SetView", signal_view)
        _wait_until(
            lambda: _string_reply(session.call("GetCurrentView")) == signal_view, "extension did not adopt ViewChanged"
        )
        _fake_daemon_call(session, "ReleaseGetView")
        assert _string_reply(session.call("GetCurrentView")) == signal_view
        assert _fake_daemon_counters(session) == (1, 1, 0)

        _fake_daemon_call(session, "SetViewSilently", _view_json(3))
        session.call("FetchView")
        _wait_until(lambda: _fake_daemon_counters(session)[0] == 2, "extension did not start second getter")
        _fake_daemon_call(session, "SetViewSilently", _view_json(4))
        _fake_daemon_call(session, "ReleaseName")
        _wait_until(
            lambda: _string_reply(session.call("GetDaemonOwner")) == "", "extension did not observe daemon shutdown"
        )
        _fake_daemon_call(session, "ReleaseGetView")
        assert _string_reply(session.call("GetCurrentView")) == signal_view

        _fake_daemon_call(session, "AcquireName")
        _wait_until(lambda: _fake_daemon_counters(session)[0] == 3, "extension did not rehydrate after restart")
        _fake_daemon_call(session, "ReleaseGetView")
        restarted_view = _view_json(4)
        _wait_until(
            lambda: _string_reply(session.call("GetCurrentView")) == restarted_view,
            "extension did not adopt restarted daemon's current view",
        )
        assert _fake_daemon_counters(session) == (3, 3, 0)
    finally:
        session.exec(
            f"if gdbus introspect --session --dest {shlex.quote(_FAKE_DBUS_DEST)} "
            f"--object-path {shlex.quote(_FAKE_DBUS_PATH)} >/dev/null 2>&1; then "
            f"gdbus call --session --dest {shlex.quote(_FAKE_DBUS_DEST)} "
            f"--object-path {shlex.quote(_FAKE_DBUS_PATH)} "
            f"--method {shlex.quote(_FAKE_DBUS_DEST + '.Stop')} >/dev/null; fi"
        )


if __name__ == "__main__":
    pytest_bazel.main()
