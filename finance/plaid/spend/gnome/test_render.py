"""Headless GNOME startup and screenshot tests for the Plaid Spend extension.

The test boots the shipped extension ZIP in a GNOME Shell session on Xvfb. It
first checks the normal startup path with the desktop daemon absent, then loads
synthetic views through a test-only D-Bus interface and captures the panel and
popup. No Plaid credentials, accounts, or live service are used.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
import pytest_bazel

from finance.plaid.spend.allowance import PeriodId
from finance.plaid.spend.models import SpendView
from finance.plaid.spend.testing import dashboard
from gnome.testing.fake_daemon import fake_daemon
from gnome.testing.session import GnomeMenuCapture, GnomeSession, gnome_session, string_reply, wait_until
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image
from util.testing.output_writer import BoundTestOutputs

_GNOME_SHELL_TEST = OciImage("_main/gnome/test_image/gnome_shell_test.rloc", "gnome-shell-test:pinned")
_EXTENSION_ZIP = "_main/finance/plaid/spend/gnome/plaid-spend-desktop.zip"
_EXTENSION_UUID = "plaid-spend@allegedly.works"
_FIXTURE_NAMES = ("ready_two_cards", "authentication_required", "offline", "retrying")
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
def allowance_scenarios() -> dict[str, SpendView]:
    dashboard_url = "https://spend.example.test/"
    ready = dashboard.spend_view()
    return {
        "allowance_paced": ready.model_copy(update={"dashboard_url": dashboard_url}),
        "allowance_warning": dashboard.spend_view(estimate_period_id=PeriodId.ROLLING_30D).model_copy(
            update={"dashboard_url": dashboard_url}
        ),
        "allowance_warming": dashboard.spend_view(warmup=True).model_copy(update={"dashboard_url": dashboard_url}),
        "allowance_exhausted": dashboard.spend_view(monthly_minor_units=49999).model_copy(
            update={"dashboard_url": dashboard_url}
        ),
    }


@pytest.fixture(scope="module")
def fixture_json_dir(tmp_path_factory: pytest.TempPathFactory, allowance_scenarios: dict[str, SpendView]) -> Path:
    dest = tmp_path_factory.mktemp("plaid-spend-fixtures")
    for fixture_name in _FIXTURE_NAMES:
        source = get_required_path(f"{_FIXTURE_DIR}/{fixture_name}.json")
        (dest / f"{fixture_name}.json").write_bytes(Path(source).read_bytes())
    for fixture_name, view in allowance_scenarios.items():
        fixture = {"status": "ready", "error": "", "view": view.model_dump(mode="json")}
        (dest / f"{fixture_name}.json").write_text(json.dumps(fixture))
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
        environment={"PLAID_SPEND_TEST": "1", "PLAID_SPEND_TEST_NOW": "2026-10-15T12:00:00.000Z"},
    ) as session:
        _assert_no_plaid_extension_error(session)
        yield session


def _card_view(amount_minor_units: int) -> str:
    return json.dumps(
        {
            "generated_at": "2026-10-15T12:00:00Z",
            "cards": [{"spend_minor_units": amount_minor_units, "currency": "USD"}],
        }
    )


def test_view_signal_wins_initial_get_and_owner_restart_rehydrates(session: GnomeSession) -> None:
    with fake_daemon(session, destination=_FAKE_DBUS_DEST, object_path=_FAKE_DBUS_PATH) as daemon:
        wait_until(lambda: daemon.counters()[0] == 1, "extension did not request the initial view")

        daemon.call("SetView", _card_view(2000))
        wait_until(
            lambda: string_reply(session.call("GetPanelLabel")) == "$20",
            "extension did not project the ViewChanged signal",
        )
        daemon.call("ReleaseGetView")
        session.wait_for_paint()
        assert daemon.counters() == (1, 1, 0)
        assert string_reply(session.call("GetPanelLabel")) == "$20"

        test_clock = datetime.fromisoformat("2026-10-15T12:00:00+00:00").timestamp()
        daemon.call("SetTiming", str(test_clock - 75), str(test_clock + 20))
        wait_until(
            lambda: all(
                text in string_reply(session.call("GetMenuText"))
                for text in ("Last attempt 1m 15s ago", "Retry in 20s")
            ),
            "extension did not project desktop retry properties",
        )

        daemon.call("SetViewSilently", _card_view(3000))
        daemon.call("ReleaseName")
        wait_until(
            lambda: string_reply(session.call("GetDaemonOwner")) == "",
            "extension did not observe the daemon going away",
        )
        assert "Last attempt" not in string_reply(session.call("GetMenuText"))
        daemon.call("AcquireName")
        wait_until(lambda: daemon.counters()[0] == 2, "extension did not rehydrate after daemon restart")
        daemon.call("ReleaseGetView")
        wait_until(
            lambda: string_reply(session.call("GetPanelLabel")) == "$30",
            "extension did not adopt the restarted daemon's view",
        )
        assert daemon.counters() == (2, 2, 0)


def _assert_no_plaid_extension_error(container: GnomeSession) -> None:
    log = container.log().decode(errors="replace")
    extension_error = f"/extensions/{_EXTENSION_UUID}/extension.js:"
    if extension_error in log or f"Extension {_EXTENSION_UUID}:" in log:
        raise AssertionError(f"GNOME logged a Plaid Spend extension error during default startup:\n{log}")


@contextmanager
def _menu(session: GnomeSession, fixture_name: str, expected_label: str) -> Iterator[tuple[int, int, int, int]]:
    session.close_menu()
    session.reload(f"/fixtures/{fixture_name}.json")
    assert string_reply(session.call("GetPanelLabel")) == expected_label
    geometry = session.open_menu()
    try:
        yield geometry
    finally:
        session.close_menu()


def _allowance_menu_text(session: GnomeSession, view: SpendView) -> str:
    menu_text = string_reply(session.call("GetMenuText"))
    assert view.allowance is not None
    assert view.cards
    assert all(card.label not in menu_text for card in view.cards)
    assert "Check a purchase / dashboard" in menu_text
    return menu_text


@pytest.fixture
def capture_menu(session: GnomeSession, test_outputs: BoundTestOutputs) -> GnomeMenuCapture:
    return GnomeMenuCapture(session, test_outputs, title="Plaid Spend GNOME extension")


@pytest.mark.parametrize(
    ("fixture_name", "expected_label"),
    [("ready_two_cards", "$149 !"), ("authentication_required", "Sign in"), ("offline", "Offline")],
)
def test_card_status(
    session: GnomeSession, fixture_name: str, expected_label: str, capture_menu: GnomeMenuCapture
) -> None:
    with _menu(session, fixture_name, expected_label) as geometry:
        capture_menu.capture(geometry, label=fixture_name.replace("_", " "))


def test_allowance_paced(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    view = allowance_scenarios["allowance_paced"]
    with _menu(session, "allowance_paced", "Flex $200") as geometry:
        menu_text = _allowance_menu_text(session, view)
        assert "Below leash" in menu_text
        assert "7d $13/day" in menu_text
        assert "30d $18/day" in menu_text
        assert "Leash ~$23/day" in menu_text
        assert "Sync 1h ago" in menu_text
        assert "View updated" not in menu_text
        assert "sustainability target" not in menu_text
        assert "past pace is not opening debt" not in menu_text
        capture_menu.capture(geometry, label="allowance paced")


def test_allowance_warning(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    view = allowance_scenarios["allowance_warning"]
    with _menu(session, "allowance_warning", "Flex $200 !") as geometry:
        menu_text = _allowance_menu_text(session, view)
        assert "Pace high" in menu_text
        assert "7d $13/day" in menu_text
        assert "30d $18/day" in menu_text
        assert "Leash ~$23/day" in menu_text
        assert "7d unmatched 1 ($15)" in menu_text
        assert "30d unmatched 1 ($15)" in menu_text
        assert "Sync 1h ago" in menu_text
        capture_menu.capture(geometry, label="allowance warning")


def test_allowance_warming(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    view = allowance_scenarios["allowance_warming"]
    allowance = view.allowance
    assert allowance is not None
    with _menu(session, "allowance_warming", "Flex $700") as geometry:
        menu_text = _allowance_menu_text(session, view)
        assert "Pace warming up" in menu_text
        assert "7d n/a" in menu_text
        assert "30d n/a" in menu_text
        assert "Leash ~$23/day" in menu_text
        assert "Sync 1h ago" in menu_text
        capture_menu.capture(geometry, label="allowance warming")


def test_allowance_exhausted(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    with _menu(session, "allowance_exhausted", "Flex -<$1 !!") as geometry:
        view = allowance_scenarios["allowance_exhausted"]
        assert "Exhausted" in _allowance_menu_text(session, view)
        capture_menu.capture(geometry, label="allowance exhausted")


def test_desktop_retry_timing_ticks_and_stays_separate_from_server_sync(
    session: GnomeSession, capture_menu: GnomeMenuCapture
) -> None:
    with _menu(session, "retrying", "Flex unavailable") as geometry:
        menu_text = string_reply(session.call("GetMenuText"))
        assert "Last attempt 1m 15s ago" in menu_text
        assert "Retry in 20s" in menu_text
        assert "Sync 1h ago" in menu_text
        session.call("AdvanceClock", "5")
        menu_text = string_reply(session.call("GetMenuText"))
        assert "Last attempt 1m 20s ago" in menu_text
        assert "Retry in 15s" in menu_text
        assert "Sync 1h ago" in menu_text
        capture_menu.capture(geometry, label="retrying")


def test_authentication_required_has_no_retry_countdown(session: GnomeSession) -> None:
    with _menu(session, "authentication_required", "Sign in"):
        menu_text = string_reply(session.call("GetMenuText"))
        assert "Last attempt 1m 15s ago" in menu_text
        assert "Sign-in required" in menu_text
        assert "Retry in" not in menu_text


if __name__ == "__main__":
    pytest_bazel.main()
