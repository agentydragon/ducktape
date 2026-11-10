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
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_view_fixture
from devinfra.oci.oci import load_oci_image
from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from gnome.test_image import gnome_shell_test
from gnome.testing.fake_daemon import fake_daemon
from gnome.testing.session import GnomeMenuCapture, GnomeSession, gnome_session, string_reply, wait_until

_EXTENSION_ZIP = "ducktape/aiquota/gnome/aiquota.zip"
_EXTENSION_UUID = "aiquota@allegedly.works"

_TEST_DBUS_DEST = "works.allegedly.AiQuotaTest"
_TEST_DBUS_PATH = "/works/allegedly/AiQuotaTest"
_FAKE_DBUS_DEST = "works.allegedly.AiQuotaFake"
_FAKE_DBUS_PATH = "/works/allegedly/AiQuotaFake"
_FAKE_DBUS_SERVICE = "ducktape/aiquota/gnome/fake-daemon.js"


def test_extension_metadata_supports_gnome_50(extension_dir: Path) -> None:
    """Keep the distributed zip loadable by wyrm2's GNOME Shell generation."""
    metadata = json.loads((extension_dir / "metadata.json").read_text())

    assert "50" in metadata["shell-version"]


@pytest.fixture(scope="module")
def gnome_shell_test_image() -> str:
    return load_oci_image(gnome_shell_test.LAYOUT_RLOCATION)


@pytest.fixture(scope="module")
def extension_dir(runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Unzip the distribution zip into a single dir for bind-mounting."""
    dest = tmp_path_factory.mktemp("claude-quota-ext")
    with zipfile.ZipFile(runfiles.root() / _EXTENSION_ZIP) as z:
        z.extractall(dest)
    return dest


@pytest.fixture(scope="module")
def fixture_json_dir(runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize the normal API view payload for the GNOME JS test hook."""
    dest = tmp_path_factory.mktemp("ai-quota-fixtures")
    runfiles_root = runfiles.root()
    for fixture_name in FIXTURE_NAMES:
        src = runfiles_root / f"ducktape/aiquota/testing/fixtures/{fixture_name}.yaml"
        data = load_quota_view_fixture(src)
        (dest / f"{fixture_name}.json").write_text(json.dumps(data, indent=2) + "\n")
    return dest


@pytest.fixture(scope="module")
def session(
    runfiles: Runfiles,
    gnome_shell_test_image: str,
    extension_dir: Path,
    fixture_json_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[GnomeSession]:
    with gnome_session(
        gnome_shell_test_image,
        extension_dir=extension_dir,
        extension=_EXTENSION_UUID,
        fixture_dir=fixture_json_dir,
        fake_daemon_script=runfiles.root() / _FAKE_DBUS_SERVICE,
        screenshot_dir=tmp_path_factory.mktemp("gnome-renders"),
        destination=_TEST_DBUS_DEST,
        object_path=_TEST_DBUS_PATH,
        environment={"AI_QUOTA_FIXTURE": f"/fixtures/{FIXTURE_NAMES[0]}.json"},
        display_size=(1280, 1024),
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
        assert string_reply(session.call("GetStreamStatus")) == "Connected"
        if fixture_name == "claude_prepaid_balance":
            header = string_reply(session.call("GetHeaderText", "claude"))
            balances = string_reply(session.call("GetBalanceText", "claude"))
            assert header == "Claude — Prepaid balance: $123.45"
            assert not balances
        elif fixture_name == "claude_reset_grants":
            grants = string_reply(session.call("GetGrantText", "claude"))
            assert "Fable resets: 2" in grants
            assert "Full resets: 1" in grants
            assert "paused" in grants
        elif fixture_name == "claude_prepaid_balance_unavailable":
            header = string_reply(session.call("GetHeaderText", "claude"))
            balances = string_reply(session.call("GetBalanceText", "claude"))
            assert header == "Claude — Prepaid balance: unavailable"
            assert not balances
        elif fixture_name == "claude_prepaid_balance_stale":
            balances = string_reply(session.call("GetBalanceText", "claude"))
            assert "Prepaid balance: $123.45" in balances
            assert "stale 1h ago" in balances
        elif fixture_name == "stale_fallback":
            header = string_reply(session.call("GetHeaderText", "claude"))
            status = string_reply(session.call("GetStatusText", "claude"))
            details = string_reply(session.call("GetDetailsText", "claude"))
            assert "Prepaid balance: $123.45" in header
            assert "Refresh failed" in status
            assert "Stale · observation" in status
            assert "HTTP 503 Service Unavailable" in details
        elif fixture_name == "openai_platform_credits":
            header = string_reply(session.call("GetHeaderText", "openai_platform"))
            grants = string_reply(session.call("GetGrantText", "openai_platform"))
            details = string_reply(session.call("GetDetailsText", "openai_platform"))
            assert header == "OpenAI API — Available balance: $125.75"
            assert "Grant: $100.00" in grants
            assert "Grant: $25.75" in grants
            assert "expires" in grants
            assert "original $100.00" in details
            assert string_reply(session.call("GetEmptyLabelText")) == "AI quota"
        elif fixture_name == "anthropic_platform_credits":
            balances = string_reply(session.call("GetBalanceText", "anthropic_platform"))
            grants = string_reply(session.call("GetGrantText", "anthropic_platform"))
            assert "Main org · Available balance: $123.45" in balances
            assert "Grant: $85.00" in grants
            assert "Promo grant" in grants
            assert "scope: Claude API (Sonnet), Claude Code" in grants
            assert "scope: unscoped" not in grants
            assert "Expired grant" not in grants
        elif fixture_name == "platform_credits_with_subscription_resets":
            assert "Prepaid balance: $25.75" in string_reply(session.call("GetHeaderText", "claude"))
            assert len(json.loads(string_reply(session.call("GetWindowRows", "claude")))) == 2

            codex_resets = string_reply(session.call("GetResetText", "codex"))
            codex_details = string_reply(session.call("GetDetailsText", "codex"))
            assert "Banked resets: 3 · next known expiry" in codex_resets
            assert "1 expiry unknown" in codex_resets
            assert "known expiries" in codex_details
            assert len(json.loads(string_reply(session.call("GetWindowRows", "codex")))) == 2

            openai_balance = string_reply(session.call("GetHeaderText", "openai_platform"))
            openai_grants = string_reply(session.call("GetGrantText", "openai_platform"))
            assert "Available balance: $60.00" in openai_balance
            assert "Grant: $60.00" in openai_grants
            assert "Jun 1" in openai_grants

            anthropic_balance = string_reply(session.call("GetBalanceText", "anthropic_platform"))
            anthropic_grants = string_reply(session.call("GetGrantText", "anthropic_platform"))
            assert "Shared org · Available balance: $75.00" in anthropic_balance
            assert "Shared org · Grant: $50.00" in anthropic_grants
            assert "Shared org · Promo grant: $25.00" in anthropic_grants
            assert "scope: Claude API (Sonnet)" in anthropic_grants
        elif fixture_name == "wire_fields":
            rows = json.loads(string_reply(session.call("GetWindowRows", "codex")))
            assert len(rows) == 1
            assert rows[0].startswith("visible (5h):")
            assert "45m" in rows[0]
            assert "pp" not in rows[0]
            assert "-55 pp" in string_reply(session.call("GetDetailsText", "codex"))
            assert "internal-only" not in "\n".join(rows)
        elif fixture_name == "binding_summary":
            assert "+10 pp" in string_reply(session.call("GetPanelText", "claude"))
            assert "+18 pp" in string_reply(session.call("GetPanelText", "codex"))
            assert "-15 pp" in string_reply(session.call("GetPanelText", "zai"))
            assert "Forecasts use average usage since the window started." in string_reply(
                session.call("GetDetailsText", "claude")
            )
        elif fixture_name == "reset_pending":
            rows = json.loads(string_reply(session.call("GetWindowRows", "codex")))
            panel = string_reply(session.call("GetPanelText", "codex"))
            assert len(rows) == 1
            assert "awaiting update" in rows[0]
            assert "exhausted" not in rows[0]
            assert panel.startswith("↻?")
            assert "⚡" not in panel
            assert "Reset time passed · awaiting updated usage" in string_reply(session.call("GetStatusText", "codex"))
        elif fixture_name == "stale_observation":
            status = string_reply(session.call("GetStatusText", "claude"))
            panel = string_reply(session.call("GetPanelText", "claude"))
            rows = json.loads(string_reply(session.call("GetWindowRows", "claude")))
            assert "Stale · observation" in status
            assert panel.startswith("~")
            assert len(rows) == 1
            assert "pace:" not in rows[0]
            session.call("SetStreamStatus", "disconnected", "cached view retained")
            assert "Stale · observation" in string_reply(session.call("GetStatusText", "claude"))
            assert string_reply(session.call("GetPanelText", "claude")).startswith("~")
            assert "cached view retained" in string_reply(session.call("GetConnectionDetails"))
        elif fixture_name == "paid_credits":
            rows = json.loads(string_reply(session.call("GetWindowRows", "codex")))
            header = string_reply(session.call("GetHeaderText", "codex"))
            resets = string_reply(session.call("GetResetText", "codex"))
            assert len(rows) == 2
            assert "Codex — Paid credits:" in header
            assert "Banked resets: 0" in resets
            assert "⚡" in string_reply(session.call("GetPanelText", "codex"))
        elif fixture_name == "observed_account_2026_11_04":
            assert len(json.loads(string_reply(session.call("GetWindowRows", "claude")))) == 1
            assert len(json.loads(string_reply(session.call("GetWindowRows", "codex")))) == 1
            assert "Claude — Prepaid balance:" in string_reply(session.call("GetHeaderText", "claude"))
            assert "Codex — Paid credits:" in string_reply(session.call("GetHeaderText", "codex"))
            assert not string_reply(session.call("GetBalanceText", "claude"))
            assert not string_reply(session.call("GetBalanceText", "codex"))
            assert json.loads(string_reply(session.call("GetPanelProviders"))) == ["claude", "codex"]
            assert len(string_reply(session.call("GetGrantText", "openai_platform")).splitlines()) == 3
            grants = string_reply(session.call("GetGrantText", "anthropic_platform"))
            assert len(grants.splitlines()) == 2
            assert re.findall(r"expires ([^·\n]+)", grants) == ["Jul 1, 2027", "Nov 25"]
            openai_grants = string_reply(session.call("GetGrantText", "openai_platform"))
            assert re.findall(r"expires ([^·\n]+)", openai_grants) == ["Dec 1", "Jan 1, 2027", "Feb 1, 2027"]
            assert "scope:" not in grants
            assert "managed-agents-onprem" in string_reply(session.call("GetDetailsText", "anthropic_platform"))
        geometry = session.open_menu()
        try:
            if fixture_name == "observed_account_2026_11_04":
                assert geometry[2] <= 480, f"account popup exceeds compact width: {geometry}"
            capture_menu.capture(geometry, label=fixture_name.replace("_", " "))
            if fixture_name == "observed_account_2026_11_04":
                session.call("SetDetailsOpen", "anthropic_platform", "true")
                expanded = session.open_menu()
                assert expanded[2] <= 480, f"expanded scope widens account popup: {expanded}"
                capture_menu.capture(expanded, label="Observed account promo details", checkpoint="promo-details")
                session.call("SetDetailsOpen", "anthropic_platform", "false")
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / f"{fixture_name}.shell.log")
        pytest.fail(f"{fixture_name}: {e}")


def test_render_distinct_prepaid_and_quota_staleness(
    session: GnomeSession, fixture_json_dir: Path, undeclared_dir: Path, capture_menu: GnomeMenuCapture
) -> None:
    try:
        data = json.loads((fixture_json_dir / "claude_prepaid_balance_stale.json").read_text())
        claude = next(provider for provider in data["providers"] if provider["provider"] == "claude")
        claude["stale_since"] = "2026-01-15T11:00:00Z"
        claude["stale_at"] = "2026-01-15T11:10:00Z"
        claude["balances"][0]["stale_since"] = "2026-01-15T10:00:00Z"
        fixture_path = fixture_json_dir / "claude_distinct_staleness.json"
        fixture_path.write_text(json.dumps(data, indent=2) + "\n")
        session.close_menu()
        session.reload("/fixtures/claude_distinct_staleness.json")

        status = string_reply(session.call("GetStatusText", "claude"))
        balances = string_reply(session.call("GetBalanceText", "claude"))
        assert "Stale · observation 1h old" in status
        assert "Prepaid balance: $123.45 · stale 2h ago" in balances

        geometry = session.open_menu()
        try:
            capture_menu.capture(geometry, label="Claude distinct staleness")
        finally:
            session.close_menu()
    except (TimeoutError, RuntimeError) as e:
        session.save_log(undeclared_dir / "claude-distinct-staleness.shell.log")
        pytest.fail(f"Claude distinct staleness: {e}")


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
        assert "\n" not in stream_status
        assert "last attempt 42s ago" in stream_status
        assert "retry in 19s" in stream_status
        assert "connection to quota api was lost" not in stream_status
        assert "connection to quota api was lost" in string_reply(session.call("GetConnectionDetails")).lower()
        session.call("SetSyncState", "958", "1019", "1001")
        stream_status = string_reply(session.call("GetStreamStatus")).lower()
        assert "\n" not in stream_status
        assert "last attempt 43s ago" in stream_status
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


def test_client_freshness_deadline_suppresses_cached_projections(session: GnomeSession, fixture_json_dir: Path) -> None:
    data = json.loads((fixture_json_dir / "binding_summary.json").read_text())
    provider = next(item for item in data["providers"] if item["provider"] == "codex")
    assert any(window["forecast"] for window in provider["display_windows"])
    provider["stale_at"] = data["fetched_at"]
    session.close_menu()
    session.reload("/fixtures/binding_summary.json")
    session.call("SetViewForTest", json.dumps(data))
    rows = string_reply(session.call("GetWindowRows", "codex"))
    assert "exhausts" not in rows
    assert "on pace" not in rows
    assert " pace:" not in string_reply(session.call("GetDetailsText", "codex"))


if __name__ == "__main__":
    pytest_bazel.main()
