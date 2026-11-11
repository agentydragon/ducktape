"""Headless GNOME startup and screenshot tests for the Plaid Spend extension.

The test boots the shipped extension ZIP in a GNOME Shell session on Xvfb. It
first checks the normal startup path with the desktop daemon absent, then loads
synthetic views through a test-only D-Bus interface and captures the panel and
popup. No Plaid credentials, accounts, or live service are used.
"""

from __future__ import annotations

import json
import re
import shlex
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from PIL import Image, ImageChops, ImageStat
from python.runfiles import Runfiles

from devinfra.oci.oci import load_oci_image
from devinfra.testing.output_writer import BoundTestOutputs
from finance.plaid.spend.allowance import PeriodId
from finance.plaid.spend.models import SpendView
from finance.plaid.spend.testing import dashboard
from gnome.test_image import gnome_shell_test
from gnome.testing.fake_daemon import fake_daemon
from gnome.testing.session import GnomeMenuCapture, GnomeSession, exec_output, gnome_session, string_reply, wait_until

_EXTENSION_ZIP = "ducktape/finance/plaid/spend/gnome/plaid-spend-desktop.zip"
_EXTENSION_UUID = "plaid-spend@allegedly.works"
_FIXTURE_NAMES = (
    "ready_allowance",
    "ready_two_cards",
    "authentication_required",
    "offline",
    "offline_cached_card",
    "offline_no_detail",
    "retrying",
)
_FIXTURE_DIR = "ducktape/finance/plaid/spend/gnome/fixtures"
_TEST_DBUS_DEST = "works.allegedly.PlaidSpendTest"
_TEST_DBUS_PATH = "/works/allegedly/PlaidSpendTest"
_FAKE_DBUS_DEST = "works.allegedly.PlaidSpendFake"
_FAKE_DBUS_PATH = "/works/allegedly/PlaidSpendFake"
_FAKE_DBUS_SERVICE = "ducktape/finance/plaid/spend/gnome/fake-daemon.js"


def test_extension_metadata_supports_gnome_50(extension_dir: Path) -> None:
    """Keep the distributed ZIP loadable by the GNOME Shell version on Rugged."""
    metadata = json.loads((extension_dir / "metadata.json").read_text())

    assert "50" in metadata["shell-version"]


@pytest.fixture(scope="module")
def gnome_shell_test_image() -> str:
    return load_oci_image(gnome_shell_test.LAYOUT_RLOCATION)


@pytest.fixture(scope="module")
def extension_dir(runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory) -> Path:
    dest = tmp_path_factory.mktemp("plaid-spend-extension")
    with zipfile.ZipFile(runfiles.root() / _EXTENSION_ZIP) as extension_zip:
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
def fixture_json_dir(
    runfiles: Runfiles, tmp_path_factory: pytest.TempPathFactory, allowance_scenarios: dict[str, SpendView]
) -> Path:
    dest = tmp_path_factory.mktemp("plaid-spend-fixtures")
    runfiles_root = runfiles.root()
    for fixture_name in _FIXTURE_NAMES:
        source = runfiles_root / f"{_FIXTURE_DIR}/{fixture_name}.json"
        (dest / f"{fixture_name}.json").write_bytes(source.read_bytes())
    auth_fixture = json.loads((dest / "authentication_required.json").read_text())
    for name, error in (
        ("auth_generic", "Sign in to connect this computer"),
        ("auth_error", "The refresh token was revoked"),
    ):
        auth_fixture["error"] = error
        (dest / f"{name}.json").write_text(json.dumps(auth_fixture))
    for fixture_name, view in allowance_scenarios.items():
        fixture = {"status": "ready", "error": "", "view": view.model_dump(mode="json")}
        (dest / f"{fixture_name}.json").write_text(json.dumps(fixture))
    fractional_view = allowance_scenarios["allowance_paced"].model_dump(mode="json")
    fractional_view["allowance"]["available_minor_units"] = 20025
    (dest / "allowance_fractional.json").write_text(
        json.dumps({"status": "ready", "error": "", "view": fractional_view})
    )
    view_payload = allowance_scenarios["allowance_paced"].model_dump(mode="json")
    allowance = view_payload["allowance"]
    allowance["available_minor_units"] = 1234500
    allowance["daily_reference_minor_units"] = 2000
    for period in allowance["recorded_pace_periods"]:
        period["observed_daily_minor_units"] = {"rolling_7d": 2200, "rolling_30d": 2800}[period["period"]["id"]]
    history = {
        "status": "active",
        "daily_reference_minor_units": 2000,
        "windows": [
            {
                "period_id": period,
                "points": [
                    {"observed_daily_minor_units": None if day == 12 else 1000 + day * step} for day in range(30)
                ],
            }
            for period, step in [("rolling_7d", 40), ("rolling_30d", 60)]
        ],
    }
    (dest / "panel_composition.json").write_text(
        json.dumps({"status": "ready", "error": "", "view": view_payload, "pace_history": history})
    )
    flat_history = {
        "status": "active",
        "daily_reference_minor_units": 2000,
        "windows": [
            {"period_id": period, "points": [{"observed_daily_minor_units": 1700} for _ in range(30)]}
            for period in ("rolling_7d", "rolling_30d")
        ],
    }
    (dest / "panel_flat.json").write_text(
        json.dumps({"status": "ready", "error": "", "view": view_payload, "pace_history": flat_history})
    )
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
        environment={"PLAID_SPEND_TEST": "1", "PLAID_SPEND_TEST_NOW": "2026-10-15T12:00:00.000Z"},
        display_size=(1920, 900),
        compile_extension_schemas=True,
        activate_display=True,
    ) as session:
        _assert_no_plaid_extension_error(session)
        yield session


def test_fixture_preview_starts_without_daemon(
    runfiles: Runfiles,
    gnome_shell_test_image: str,
    extension_dir: Path,
    fixture_json_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    with gnome_session(
        gnome_shell_test_image,
        extension_dir=extension_dir,
        extension=_EXTENSION_UUID,
        fixture_dir=fixture_json_dir,
        fake_daemon_script=runfiles.root() / _FAKE_DBUS_SERVICE,
        screenshot_dir=tmp_path_factory.mktemp("gnome-fixture-preview"),
        destination=_TEST_DBUS_DEST,
        object_path=_TEST_DBUS_PATH,
        environment={"PLAID_SPEND_FIXTURE": "/fixtures/ready_allowance.json"},
        compile_extension_schemas=True,
    ) as session:
        assert string_reply(session.call("GetDaemonOwner")) == ""

        def panel_state() -> dict[str, Any]:
            return cast(dict[str, Any], json.loads(string_reply(session.call("GetPanelState"))))

        def set_visible(value: str) -> None:
            result = session.exec(
                f"gsettings --schemadir /usr/share/gnome-shell/extensions/{_EXTENSION_UUID}/schemas "
                f"set org.gnome.shell.extensions.plaid-spend panel-visible {value!r}"
            )
            assert result.exit_code == 0, exec_output(result)

        set_visible("['pace', 'sparkline']")
        wait_until(lambda: panel_state()["items"] == ["pace", "sparkline"], "fixture settings were not applied")
        assert string_reply(session.call("GetPanelLabel")) == "7d 90%"
        set_visible("['pace']")
        wait_until(lambda: panel_state()["items"] == ["pace"], "sparkline was not hidden")
        set_visible("['pace', 'sparkline']")
        wait_until(lambda: panel_state()["items"] == ["pace", "sparkline"], "fixture history was not retained")
        _assert_no_plaid_extension_error(session)


def test_host_daemon_uses_separate_session_bus(
    runfiles: Runfiles,
    gnome_shell_test_image: str,
    extension_dir: Path,
    fixture_json_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    host_bus = "unix:path=/tmp/plaid-host-bus"

    def host_command(session: GnomeSession, command: str) -> bytes:
        result = session.exec(f"DBUS_SESSION_BUS_ADDRESS={host_bus} {command}")
        stdout, stderr = exec_output(result)
        assert result.exit_code == 0, (stdout, stderr)
        return stdout

    def host_call(session: GnomeSession, method: str, *args: str) -> bytes:
        return host_command(
            session,
            f"gdbus call --session --dest {_FAKE_DBUS_DEST} --object-path {_FAKE_DBUS_PATH} "
            f"--method {_FAKE_DBUS_DEST}.{method} " + " ".join(shlex.quote(arg) for arg in args),
        )

    def start_host(session: GnomeSession) -> None:
        result = session.exec(f"dbus-daemon --session --fork --address={host_bus}")
        assert result.exit_code == 0, exec_output(result)
        host_command(session, "gjs -m /fake-daemon.js >/tmp/host-fake-daemon.log 2>&1 &")
        wait_until(
            lambda: (
                session.exec(
                    f"DBUS_SESSION_BUS_ADDRESS={host_bus} gdbus introspect --session "
                    f"--dest {_FAKE_DBUS_DEST} --object-path {_FAKE_DBUS_PATH}"
                ).exit_code
                == 0
            ),
            "host fake daemon did not start",
        )

    with gnome_session(
        gnome_shell_test_image,
        extension_dir=extension_dir,
        extension=_EXTENSION_UUID,
        fixture_dir=fixture_json_dir,
        fake_daemon_script=runfiles.root() / _FAKE_DBUS_SERVICE,
        screenshot_dir=tmp_path_factory.mktemp("gnome-host-daemon"),
        destination=_TEST_DBUS_DEST,
        object_path=_TEST_DBUS_PATH,
        environment={"PLAID_SPEND_TEST": "1", "PLAID_SPEND_DEVKIT_HOST_BUS_ADDRESS": host_bus},
        compile_extension_schemas=True,
        before_start=start_host,
    ) as session:

        def pending_view() -> bool:
            return bool(re.search(rb"\(uint32 \d+, uint32 \d+, uint32 [1-9]\d*\)", host_call(session, "GetCounters")))

        wait_until(lambda: bool(string_reply(session.call("GetDaemonOwner"))), "host daemon was not connected")
        wait_until(pending_view, "host view was not requested")
        host_call(session, "ReleaseGetView")
        wait_until(lambda: string_reply(session.call("GetPanelLabel")) == "$10", "host view was not loaded")
        host_call(session, "SetView", _card_view(2000))
        wait_until(lambda: string_reply(session.call("GetPanelLabel")) == "$20", "host update was not received")
        host_call(session, "ReleaseName")
        wait_until(lambda: not string_reply(session.call("GetDaemonOwner")), "host daemon loss was not observed")
        host_call(session, "AcquireName")
        wait_until(lambda: bool(string_reply(session.call("GetDaemonOwner"))), "host daemon did not reconnect")
        wait_until(pending_view, "host view was not requested after reconnect")
        host_call(session, "ReleaseGetView")
        wait_until(lambda: string_reply(session.call("GetPanelLabel")) == "$20", "host view was not reloaded")
        _assert_no_plaid_extension_error(session)


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


def test_status_icons_exist_in_the_gnome_theme(session: GnomeSession) -> None:
    result = session.exec(
        'gjs -c \'imports.gi.versions.Gtk = "4.0"; '
        "const { Gtk, Gdk } = imports.gi; Gtk.init(); "
        "const theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default()); "
        'for (const name of ["object-select-symbolic", "dialog-warning-symbolic", '
        '"dialog-error-symbolic", "dialog-question-symbolic"]) '
        "if (!theme.has_icon(name)) throw new Error(`Missing icon: ${name}`);'"
    )
    assert result.exit_code == 0, exec_output(result)


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
    [
        ("ready_two_cards", "$149"),
        ("authentication_required", "Sign in"),
        ("offline", "Offline"),
        ("offline_cached_card", "Offline"),
        ("offline_no_detail", "Offline"),
    ],
)
def test_card_status(
    session: GnomeSession, fixture_name: str, expected_label: str, capture_menu: GnomeMenuCapture
) -> None:
    with _menu(session, fixture_name, expected_label) as geometry:
        menu_text = string_reply(session.call("GetMenuText"))
        if fixture_name == "ready_two_cards":
            assert json.loads(string_reply(session.call("GetPanelState")))["level"] == "caution"
        elif fixture_name == "offline":
            assert "Connection problem" not in menu_text
            assert "figures may be stale" not in menu_text
            assert "No card data is available." in menu_text
            assert "Details: The Plaid Spend service is unavailable." in menu_text
        elif fixture_name == "offline_cached_card":
            assert "Connection problem · figures may be stale" in menu_text
            assert "Spend: $123 / no limit set" in menu_text
            assert "Details: The network is unavailable." in menu_text
        elif fixture_name == "offline_no_detail":
            assert "Connection problem" in menu_text
            assert "figures may be stale" not in menu_text
            assert "No card data is available." in menu_text
            assert "Details:" not in menu_text
        capture_menu.capture(geometry, label=fixture_name.replace("_", " "))


def test_allowance_paced(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    view = allowance_scenarios["allowance_paced"]
    with _menu(session, "allowance_paced", "$200") as geometry:
        menu_text = _allowance_menu_text(session, view)
        assert "$200 available" in menu_text
        assert "Balance details" not in menu_text
        assert "7d pace ≤ leash" in menu_text
        assert "7d $13/day" in menu_text
        assert "30d $18/day" in menu_text
        assert "Leash ~$23/day" in menu_text
        assert "Sync 1h ago" in menu_text
        assert "View updated" not in menu_text
        assert "sustainability target" not in menu_text
        assert "past pace is not opening debt" not in menu_text
        capture_menu.capture(geometry, label="allowance paced")


def test_rounded_balance_keeps_exact_amount_in_details(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
    with _menu(session, "allowance_fractional", "$200"):
        menu_text = string_reply(session.call("GetMenuText"))
        assert "$200 available" in menu_text
        assert "$200.25" not in menu_text
        assert "Balance details" in menu_text
        assert string_reply(session.call("GetBalanceDetails")) == "Exact available: $200.25"
        session.call("OpenBalanceDetails")
        session.wait_for_paint()
        capture_menu.capture(session.open_menu(), label="exact balance details")


def test_allowance_warning(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    view = allowance_scenarios["allowance_warning"]
    with _menu(session, "allowance_warning", "$200") as geometry:
        menu_text = _allowance_menu_text(session, view)
        assert "7d pace ≤ leash" in menu_text
        assert "Overall warning · 30d forecast or pace" in menu_text
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
    with _menu(session, "allowance_warming", "$700") as geometry:
        menu_text = _allowance_menu_text(session, view)
        assert "7d pace unavailable" in menu_text
        assert "7d n/a" in menu_text
        assert "30d n/a" in menu_text
        assert "Leash ~$23/day" in menu_text
        assert "Sync 1h ago" in menu_text
        capture_menu.capture(geometry, label="allowance warming")


def test_allowance_exhausted(
    session: GnomeSession, capture_menu: GnomeMenuCapture, allowance_scenarios: dict[str, SpendView]
) -> None:
    with _menu(session, "allowance_exhausted", "-$0.01") as geometry:
        view = allowance_scenarios["allowance_exhausted"]
        assert "Allowance exhausted" in _allowance_menu_text(session, view)
        capture_menu.capture(geometry, label="allowance exhausted")


def test_desktop_retry_timing_ticks_and_stays_separate_from_server_sync(
    session: GnomeSession, capture_menu: GnomeMenuCapture
) -> None:
    with _menu(session, "retrying", "Offline") as geometry:
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
        assert "Sign in to see flexible spending" not in menu_text
        assert "Sign in with Authentik" in menu_text
        assert "Last attempt 1m 15s ago" in menu_text
        assert "Sign-in required" not in menu_text
        assert "Your account is not connected." not in menu_text
        assert "Details: Sign in to connect your Plaid account." not in menu_text
        assert "Retry in" not in menu_text


@pytest.mark.parametrize(
    ("fixture_name", "detail"), [("auth_generic", ""), ("auth_error", "Details: The refresh token was revoked")]
)
def test_sign_in_keeps_distinct_errors(session: GnomeSession, fixture_name: str, detail: str) -> None:
    with _menu(session, fixture_name, "Sign in"):
        menu_text = string_reply(session.call("GetMenuText"))
        assert "Sign in with Authentik" in menu_text
        if detail:
            assert detail in menu_text
        else:
            assert "Details:" not in menu_text


def test_custom_panel_items_and_pace_window(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
    def configure(**settings: object) -> None:
        session.call("SetPanelSettings", json.dumps(settings))
        session.wait_for_paint()

    def state() -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(string_reply(session.call("GetPanelState"))))

    try:
        configure(
            order=["pace", "sparkline", "status", "balance"],
            visible=["status", "balance", "pace", "sparkline"],
            window="7d",
        )
        with _menu(session, "panel_composition", "7d 110% $12.3k") as geometry:
            assert state() == {"items": ["pace", "sparkline", "status", "balance"], "level": "caution", "window": "7d"}
            assert "$12,345 available" in string_reply(session.call("GetMenuText"))
            assert "Customize panel" in string_reply(session.call("GetMenuText"))
            assert "Pace history has coverage gaps" in string_reply(session.call("GetMenuText"))
            capture_menu.capture(geometry, label="custom panel 7d with sparkline", checkpoint="7d", left_padding=160)
        configure(window="30d")
        assert string_reply(session.call("GetPanelLabel")) == "30d 140% $12.3k"
        assert state()["level"] == "danger"
        geometry = session.open_menu()
        capture_menu.capture(geometry, label="custom panel 30d with sparkline", checkpoint="30d", left_padding=160)
        session.close_menu()
        configure(order=["sparkline", "pace", "balance", "status"], visible=["sparkline", "pace"])
        assert state()["items"] == ["sparkline", "pace"]
        session.reload("/fixtures/allowance_paced.json")
        assert state()["items"] == ["sparkline-unavailable", "pace"]
        assert state()["level"] == "good"
        configure(visible=[])
        assert state()["items"] == ["status"]
        _assert_no_plaid_extension_error(session)
    finally:
        configure(order=["status", "balance", "pace", "sparkline"], visible=["status", "balance"], window="7d")


def test_sparkline_options(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
    def configure(**settings: object) -> None:
        session.call("SetPanelSettings", json.dumps(settings))
        session.wait_for_paint()

    def sparkline_image() -> Image.Image:
        match = re.search(rb"\((-?\d+),\s*(-?\d+),\s*(\d+),\s*(\d+)\)", session.call("GetSparklineGeometry"))
        assert match is not None
        x, y, width, height = (int(value) for value in match.groups())
        assert width > 0
        assert height > 0
        return Image.open(BytesIO(session.screenshot())).convert("RGB").crop((x, y, x + width, y + height))

    try:
        configure(visible=["sparkline"], window="30d", anchorZero=True, showLeash=True, historyDays=30)
        session.reload("/fixtures/panel_composition.json")
        geometry = session.open_menu()
        assert "Sparkline: last 30 days of 30d averages" in string_reply(session.call("GetMenuText"))
        zero_with_leash = sparkline_image()
        capture_menu.capture(geometry, label="sparkline at zero with leash", checkpoint="zero-leash", left_padding=160)
        configure(leashColor="#00ffff")
        custom_leash = sparkline_image()
        assert ImageChops.difference(zero_with_leash, custom_leash).getbbox() is not None
        pixels = cast(list[tuple[int, int, int]], list(custom_leash.get_flattened_data()))
        assert any(g > r + 40 and b > r + 40 for r, g, b in pixels)
        capture_menu.capture(
            geometry, label="sparkline custom leash color", checkpoint="custom-leash", left_padding=160
        )
        configure(leashColor="")
        configure(anchorZero=False)
        fitted_with_leash = sparkline_image()
        assert ImageChops.difference(zero_with_leash, fitted_with_leash).getbbox() is not None
        capture_menu.capture(geometry, label="sparkline fitted with leash", checkpoint="fitted-leash", left_padding=160)
        configure(showLeash=False)
        fitted_without_leash = sparkline_image()
        assert ImageChops.difference(fitted_with_leash, fitted_without_leash).getbbox() is not None
        pixels = cast(list[tuple[int, int, int]], list(fitted_without_leash.get_flattened_data()))
        assert any(g > r + 12 and g > b + 12 for r, g, b in pixels)
        assert any(r > g + 10 and g > b + 20 for r, g, b in pixels)
        assert any(r > g + 30 and r > b + 30 for r, g, b in pixels)
        capture_menu.capture(geometry, label="sparkline fitted without leash", checkpoint="fitted", left_padding=160)
        configure(historyDays=7)
        assert "Sparkline: last 7 days of 30d averages" in string_reply(session.call("GetMenuText"))
        assert ImageChops.difference(fitted_without_leash, sparkline_image()).getbbox() is not None
        capture_menu.capture(geometry, label="sparkline seven-day history", checkpoint="seven-days", left_padding=160)
        session.reload("/fixtures/panel_flat.json")
        flat_pixels = cast(list[tuple[int, int, int]], list(sparkline_image().get_flattened_data()))
        assert any(g > r + 12 and g > b + 12 for r, g, b in flat_pixels)
        session.close_menu()
        _assert_no_plaid_extension_error(session)
    finally:
        configure(
            visible=["status", "balance"], window="7d", anchorZero=True, showLeash=True, leashColor="", historyDays=30
        )


def test_history_from_daemon_drops_stale_replies(session: GnomeSession, fixture_json_dir: Path) -> None:
    fixture = json.loads((fixture_json_dir / "panel_composition.json").read_text())
    session.reload("/fixtures/panel_composition.json")
    try:
        with fake_daemon(session, destination=_FAKE_DBUS_DEST, object_path=_FAKE_DBUS_PATH) as daemon:
            wait_until(lambda: bool(string_reply(session.call("GetDaemonOwner"))), "daemon did not connect")
            daemon.call("SetHistory", json.dumps(fixture["pace_history"]))
            session.call("SetPanelSettings", json.dumps({"visible": ["sparkline"]}))

            def pending() -> int:
                return int(string_reply(daemon.call("GetHistoryPending")))

            wait_until(lambda: pending() == 1, "extension did not request pace history")
            daemon.call("ReleaseGetHistory")
            wait_until(
                lambda: json.loads(string_reply(session.call("GetPanelState")))["items"] == ["sparkline"],
                "extension did not draw daemon history",
            )

            daemon.call("SetView", json.dumps(fixture["view"]))
            wait_until(lambda: pending() == 1, "extension did not refresh history")
            daemon.call("SetView", json.dumps(fixture["view"]))
            wait_until(lambda: pending() == 2, "extension did not request the new history")
            daemon.call("ReleaseGetHistory")
            session.wait_for_paint()
            assert json.loads(string_reply(session.call("GetPanelState")))["items"] == ["sparkline-unavailable"]
            daemon.call("ReleaseGetHistory")
            wait_until(
                lambda: json.loads(string_reply(session.call("GetPanelState")))["items"] == ["sparkline"],
                "extension did not adopt the latest history",
            )
            _assert_no_plaid_extension_error(session)
    finally:
        session.call("SetPanelSettings", json.dumps({"visible": ["status", "balance"]}))


def test_preferences_window_opens(session: GnomeSession, test_outputs: BoundTestOutputs) -> None:
    session.close_menu()
    session.call("HideOverview")
    wait_until(lambda: b"false" in session.call("GetOverviewVisible"), "GNOME overview did not close")
    before = Image.open(BytesIO(session.screenshot())).convert("RGB").crop((480, 80, 1440, 820))
    session.call("OpenPreferences")

    def preferences_visible() -> bool:
        after = Image.open(BytesIO(session.screenshot())).convert("RGB").crop((480, 80, 1440, 820))
        return sum(ImageStat.Stat(ImageChops.difference(before, after)).sum) > 1_000_000

    try:
        wait_until(preferences_visible, "GNOME did not show the panel preferences")
    except TimeoutError as error:
        result = session.exec(
            "gdbus call --session --timeout 10 --dest org.gnome.Shell.Extensions "
            "--object-path /org/gnome/Shell/Extensions --method org.gnome.Shell.Extensions.OpenExtensionPrefs "
            "'plaid-spend@allegedly.works' '' '{}'"
        )
        raise AssertionError(f"Preferences service call failed: {exec_output(result)}") from error
    test_outputs.publish(
        checkpoint="sparkline-disabled",
        role="screenshot",
        data=session.screenshot(),
        suffix="png",
        label="panel preferences",
        title="Plaid Spend GNOME extension",
    )
    disabled = Image.open(BytesIO(session.screenshot())).convert("RGB").crop((480, 80, 1440, 820))
    session.call("SetPanelSettings", json.dumps({"visible": ["status", "balance", "sparkline"]}))

    def sparkline_controls_enabled() -> bool:
        enabled = Image.open(BytesIO(session.screenshot())).convert("RGB").crop((480, 80, 1440, 820))
        return sum(ImageStat.Stat(ImageChops.difference(disabled, enabled)).sum) > 10_000

    wait_until(sparkline_controls_enabled, "preferences did not update when sparkline was enabled")
    test_outputs.publish(
        checkpoint="sparkline-enabled",
        role="screenshot",
        data=session.screenshot(),
        suffix="png",
        label="panel preferences with sparkline enabled",
        title="Plaid Spend GNOME extension",
    )


if __name__ == "__main__":
    pytest_bazel.main()
