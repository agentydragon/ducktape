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
visual-review page via structured output records (see
devinfra/pr_visuals/plans/goldens_to_pr_visuals.md); the hard gate is
that gnome-shell boots, the extension reaches ENABLED, and every
fixture opens its menu and screenshots successfully.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_bazel

from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_view_fixture
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.gnome.fake_daemon import fake_daemon
from util.testing.gnome.session import GnomeMenuCapture, GnomeSession, gnome_session, string_reply, wait_until
from util.testing.output_writer import BoundTestOutputs
from util.testing.undeclared_outputs import undeclared_outputs_dir

_GNOME_SHELL_TEST = OciImage("_main/gnome/test_image/gnome_shell_test.rloc", "gnome-shell-test:pinned")
_EXTENSION_ZIP = "_main/aiquota/gnome/aiquota.zip"
_EXTENSION_UUID = "aiquota@allegedly.works"

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
def session(
    gnome_shell_test_image: str, extension_dir: Path, fixture_json_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[GnomeSession]:
    with gnome_session(
        gnome_shell_test_image,
        extension_dir=extension_dir,
        extension=_EXTENSION_UUID,
        fixture_dir=fixture_json_dir,
        fake_daemon_script=get_required_path(_FAKE_DBUS_SERVICE),
        screenshot_dir=tmp_path_factory.mktemp("gnome-renders"),
        destination=_TEST_DBUS_DEST,
        object_path=_TEST_DBUS_PATH,
        environment={"AI_QUOTA_FIXTURE": f"/fixtures/{FIXTURE_NAMES[0]}.json"},
    ) as session:
        yield session


@pytest.fixture
def undeclared_dir() -> Path:
    out = undeclared_outputs_dir()
    out.mkdir(parents=True, exist_ok=True)
    return out


@pytest.fixture
def capture_menu(session: GnomeSession, test_outputs: BoundTestOutputs) -> GnomeMenuCapture:
    return GnomeMenuCapture(session, test_outputs, title="AI quota GNOME extension")


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_render(session: GnomeSession, undeclared_dir: Path, fixture_name: str, capture_menu: GnomeMenuCapture) -> None:
    try:
        session.close_menu()
        session.reload(f"/fixtures/{fixture_name}.json")
        if fixture_name == "claude_prepaid_balance":
            prepaid_text = string_reply(session.call("GetPrepaidText", "claude"))
            assert prepaid_text.startswith("Claude prepaid balance")
            assert prepaid_text.endswith("$123.45")
        elif fixture_name == "claude_prepaid_balance_unavailable":
            assert string_reply(session.call("GetPrepaidText", "claude")) == "Claude prepaid balance unavailable"
        elif fixture_name == "wire_fields":
            rows = json.loads(string_reply(session.call("GetWindowRows", "codex")))
            assert len(rows) == 1
            assert rows[0].startswith("visible (5h):")
            assert "45m" in rows[0]
            assert "-55%" in rows[0]
            assert "internal-only" not in "\n".join(rows)
        geometry = session.open_menu()
        try:
            capture_menu.capture(geometry, label=fixture_name.replace("_", " "))
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / f"{fixture_name}.shell.log")
        pytest.fail(f"{fixture_name}: {e}")


def test_render_disconnected_stream(
    session: GnomeSession, undeclared_dir: Path, capture_menu: GnomeMenuCapture
) -> None:
    try:
        session.close_menu()
        session.reload("/fixtures/hot.json")
        session.call("SetStreamStatus", "disconnected", "Connection to quota API was lost")
        session.call("SetSyncState", "958", "1019", "1000")
        session.wait_for_paint()
        stream_status = string_reply(session.call("GetStreamStatus")).lower()
        assert "desktop sync attempt 42s ago" in stream_status
        assert "retry in 19s" in stream_status
        session.call("SetSyncState", "958", "1019", "1001")
        stream_status = string_reply(session.call("GetStreamStatus")).lower()
        assert "desktop sync attempt 43s ago" in stream_status
        assert "retry in 18s" in stream_status
        session.call("SetSyncState", "958", "1019", "1000")
        session.wait_for_paint()
        geometry = session.open_menu()
        try:
            capture_menu.capture(geometry, label="stream disconnected")
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / "stream_disconnected.shell.log")
        pytest.fail(f"stream disconnected: {e}")


def test_render_disconnected_without_snapshot(
    session: GnomeSession, undeclared_dir: Path, capture_menu: GnomeMenuCapture
) -> None:
    try:
        session.close_menu()
        session.reload(f"/fixtures/{FIXTURE_NAMES[0]}.json")
        empty_view = json.dumps({"fetched_at": "2026-10-18T12:00:00Z", "providers": []})
        session.call("SetViewForTest", empty_view)
        session.call("SetStreamStatus", "disconnected", "AIQuota daemon is not running")
        session.wait_for_paint()
        geometry = session.open_menu()
        try:
            capture_menu.capture(geometry, label="stream disconnected without snapshot")
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / "stream_disconnected_without_snapshot.shell.log")
        pytest.fail(f"stream disconnected without snapshot: {e}")


def _view_json(revision: int) -> str:
    return json.dumps({"fetched_at": f"2026-10-18T12:00:0{revision}Z", "providers": []})


def test_view_signal_wins_getter_and_owner_restart_invalidates_reply(session: GnomeSession) -> None:
    with fake_daemon(session, destination=_FAKE_DBUS_DEST, object_path=_FAKE_DBUS_PATH) as daemon:
        session.call("ConnectDaemon")
        wait_until(lambda: daemon.counters()[0] == 1, "extension did not request initial view")

        signal_view = _view_json(2)
        daemon.call("SetView", signal_view)
        wait_until(
            lambda: string_reply(session.call("GetCurrentView")) == signal_view, "extension did not adopt ViewChanged"
        )
        daemon.call("ReleaseGetView")
        assert string_reply(session.call("GetCurrentView")) == signal_view
        assert daemon.counters() == (1, 1, 0)

        daemon.call("SetViewSilently", _view_json(3))
        session.call("FetchView")
        wait_until(lambda: daemon.counters()[0] == 2, "extension did not start second getter")
        daemon.call("SetViewSilently", _view_json(4))
        daemon.call("ReleaseName")
        wait_until(
            lambda: string_reply(session.call("GetDaemonOwner")) == "", "extension did not observe daemon shutdown"
        )
        daemon.call("ReleaseGetView")
        assert string_reply(session.call("GetCurrentView")) == signal_view

        daemon.call("AcquireName")
        wait_until(lambda: daemon.counters()[0] == 3, "extension did not rehydrate after restart")
        daemon.call("ReleaseGetView")
        restarted_view = _view_json(4)
        wait_until(
            lambda: string_reply(session.call("GetCurrentView")) == restarted_view,
            "extension did not adopt restarted daemon's current view",
        )
        assert daemon.counters() == (3, 3, 0)


if __name__ == "__main__":
    pytest_bazel.main()
