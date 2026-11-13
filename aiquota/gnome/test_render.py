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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
import pytest_bazel
from python.runfiles import Runfiles

from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_view_fixture, meter_quota_view
from devinfra.oci.oci import load_oci_image
from devinfra.testing.output_writer import BoundTestOutputs
from gnome.test_image import gnome_shell_test
from gnome.testing.fake_daemon import fake_daemon
from gnome.testing.session import GnomeMenuCapture, GnomeSession, gnome_session, string_reply, wait_until

_EXTENSION_ZIP = "ducktape/aiquota/gnome/aiquota.zip"
_EXTENSION_UUID = "aiquota@allegedly.works"
_EMPTY_FIXTURE = "empty"

_TEST_DBUS_DEST = "works.allegedly.AiQuotaTest"
_TEST_DBUS_PATH = "/works/allegedly/AiQuotaTest"
_FAKE_DBUS_DEST = "works.allegedly.AiQuotaFake"
_FAKE_DBUS_PATH = "/works/allegedly/AiQuotaFake"
_FAKE_DBUS_SERVICE = "ducktape/aiquota/gnome/fake-daemon.js"


@dataclass(frozen=True)
class _ProviderPopup:
    session: GnomeSession
    provider: str

    def text(self, method: str) -> str:
        return string_reply(self.session.call(method, self.provider))

    @property
    def header(self) -> str:
        return self.text("GetHeaderText")

    @property
    def balance(self) -> str:
        return self.text("GetBalanceText")

    @property
    def grants(self) -> str:
        return self.text("GetGrantText")

    @property
    def status(self) -> str:
        return self.text("GetStatusText")

    @property
    def burn(self) -> str:
        return self.text("GetBurnText")

    @property
    def reset(self) -> str:
        return self.text("GetResetText")

    @property
    def panel(self) -> str:
        return self.text("GetPanelText")

    @property
    def windows(self) -> list[str]:
        return cast(list[str], json.loads(self.text("GetWindowRows")))

    @property
    def groups(self) -> list[dict[str, str]]:
        return cast(list[dict[str, str]], json.loads(self.text("GetReadingGroups")))

    @property
    def details(self) -> str:
        return self.text("GetDetailsText")


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
        environment={"AI_QUOTA_FIXTURE": f"/fixtures/{_EMPTY_FIXTURE}.json"},
        display_size=(1280, 1024),
    ) as session:
        yield session


@pytest.fixture
def capture_menu(session: GnomeSession, test_outputs: BoundTestOutputs) -> GnomeMenuCapture:
    return GnomeMenuCapture(session, test_outputs, title="AI quota GNOME extension")


def _reload_fixture(session: GnomeSession, fixture_name: str) -> None:
    session.close_menu()
    session.reload(f"/fixtures/{fixture_name}.json")
    assert string_reply(session.call("GetStreamStatus")) == "Connected"


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_render(session: GnomeSession, fixture_name: str, capture_menu: GnomeMenuCapture) -> None:
    _reload_fixture(session, fixture_name)
    geometry = session.open_menu()
    try:
        capture_menu.capture(geometry, label=fixture_name.replace("_", " "))
    finally:
        session.close_menu()


@pytest.mark.parametrize(
    ("scenario", "default_visible", "known"),
    [("ordinary", False, True), ("exhausted", True, True), ("pending", False, True), ("unknown", False, False)],
)
def test_banked_reset_visibility(
    session: GnomeSession, capture_menu: GnomeMenuCapture, scenario: str, default_visible: bool, known: bool
) -> None:
    _reload_fixture(session, f"banked_resets_{scenario}")
    popup = _ProviderPopup(session, "codex")
    assert (popup.reset == "Banked resets: 0") == default_visible
    assert ("Banked resets: 0" in popup.details) == known
    if known:
        session.open_menu()
        try:
            session.call("SetDetailsOpen", "codex", "true")
            session.wait_for_paint()
            capture_menu.capture(
                session.open_menu(), label=f"{scenario} zero reset details", checkpoint="reset-details"
            )
        finally:
            session.close_menu()


def test_zero_resets_move_into_details_when_a_different_window_passes_reset(session: GnomeSession) -> None:
    _reload_fixture(session, "banked_resets_exhausted")
    popup = _ProviderPopup(session, "codex")
    assert popup.reset == "Banked resets: 0"
    session.call("SetPresentationTime", str(datetime.fromisoformat("2026-01-15T12:01:00Z").timestamp() * 1000))
    assert not popup.reset
    assert "Banked resets: 0" in popup.details
    session.call("SetPresentationTime", str(datetime.fromisoformat("2026-01-15T12:11:00Z").timestamp() * 1000))
    assert "Stale" in popup.status
    assert "Banked resets: 0" in popup.details


def test_claude_prepaid_balance(session: GnomeSession) -> None:
    _reload_fixture(session, "claude_prepaid_balance")
    popup = _ProviderPopup(session, "claude")
    assert popup.header == "Claude — Available balance: $123"
    assert "Claude prepaid credits" in popup.details
    assert not popup.balance


@pytest.mark.parametrize(
    ("usage", "displayed", "value"), [(-5, -5, 0), (0, 0, 0), (99.9, 99, 99), (100, 100, 100), (105, 105, 100)]
)
def test_meter_accessibility(session: GnomeSession, usage: float, displayed: int, value: int) -> None:
    _reload_fixture(session, "binding_summary")
    view = meter_quota_view(usage)
    session.call("SetViewForTest", view.model_dump_json())
    session.open_menu()
    try:
        meters = json.loads(string_reply(session.call("GetMeterAccessibility", "codex")))
        assert meters[0] == {
            "role": "level bar",
            "name": "5h quota usage",
            "description": f"{displayed}% used, 50% of the window elapsed. Fill is quota used; tick is time elapsed.",
            "value": value,
            "minimum": 0,
            "maximum": 100,
        }
    finally:
        session.close_menu()


def test_claude_reset_grants(session: GnomeSession) -> None:
    _reload_fixture(session, "claude_reset_grants")
    grants = _ProviderPopup(session, "claude").grants
    assert "Fable resets: 2 left" in grants
    assert "Full resets: 1 left" in grants
    assert "paused" in grants


def test_claude_prepaid_balance_unavailable(session: GnomeSession) -> None:
    _reload_fixture(session, "claude_prepaid_balance_unavailable")
    popup = _ProviderPopup(session, "claude")
    assert popup.header == "Claude — Available balance: unavailable"
    assert not popup.balance


def test_claude_prepaid_balance_stale(session: GnomeSession) -> None:
    _reload_fixture(session, "claude_prepaid_balance_stale")
    balance = _ProviderPopup(session, "claude").balance
    assert "Available balance: $123" in balance
    assert "stale 1h ago" in balance


def test_stale_fallback(session: GnomeSession) -> None:
    _reload_fixture(session, "stale_fallback")
    popup = _ProviderPopup(session, "claude")
    assert "Available balance: $123" in popup.header
    status = popup.status
    assert "Refresh failed" in status
    assert "Stale · observation" in status
    assert "HTTP 503 Service Unavailable" in popup.details


def test_warnings_precede_readings_and_spend_precedes_windows(session: GnomeSession) -> None:
    _reload_fixture(session, "stale_fallback")
    session.open_menu()
    try:
        positions = json.loads(string_reply(session.call("GetRowPositions", "claude")))
        assert positions["status"] < positions["window"]
    finally:
        session.close_menu()

    _reload_fixture(session, "hot")
    popup = _ProviderPopup(session, "claude")
    assert "Extra usage enabled" not in popup.status
    assert "Extra spend this month: $3,142 of $5,000 limit · extra usage enabled" in popup.text("GetSpendText")
    assert any("100%" in window for window in popup.windows)
    assert "⚡" in popup.panel

    _reload_fixture(session, "extra_enabled_not_burning")
    session.open_menu()
    try:
        popup = _ProviderPopup(session, "claude")
        assert "Extra spend" not in popup.status
        assert "Extra spend this month: $2,325" in popup.text("GetSpendText")
        positions = json.loads(popup.text("GetRowPositions"))
        assert positions["spend"] < positions["window"]
    finally:
        session.close_menu()


@pytest.mark.parametrize("fixture_name", ["extra_disabled_positive", "extra_spend_fallback"])
def test_monthly_spend_is_history_even_when_disabled_or_refresh_failed(
    session: GnomeSession, fixture_name: str
) -> None:
    _reload_fixture(session, fixture_name)
    popup = _ProviderPopup(session, "claude")
    assert "Extra spend this month: $26 of $100 limit" in popup.text("GetSpendText")
    assert "Extra spend" not in popup.status
    assert "No quota or balance readings" not in popup.status
    if popup.windows:
        session.open_menu()
        try:
            positions = json.loads(popup.text("GetRowPositions"))
            assert positions["spend"] < positions["window"]
        finally:
            session.close_menu()
    assert "extra usage enabled" not in popup.status
    assert "⚡" not in popup.panel
    if fixture_name == "extra_spend_fallback":
        assert "Stale · observation" in popup.status
        assert "Refresh failed" in popup.status


@pytest.mark.parametrize("fixture_name", ["multi_window_reset_pending", "stale_paid_signals"])
def test_uncertain_paid_signals_are_suppressed_across_windows(session: GnomeSession, fixture_name: str) -> None:
    _reload_fixture(session, fixture_name)
    for provider in ("claude", "codex"):
        popup = _ProviderPopup(session, provider)
        assert "⚡" not in popup.panel
        assert "extra usage enabled" not in popup.status
        assert "paid credits available" not in popup.status
        if fixture_name == "multi_window_reset_pending":
            assert popup.panel.startswith("↻?")
            assert "awaiting updated usage" in popup.status
        else:
            assert popup.panel.startswith("~")
            assert "Stale · observation" in popup.status


@pytest.mark.parametrize("deadline", ["stale_at", "reset_at"])
def test_disconnected_view_drops_paid_signal_at_live_deadline(
    session: GnomeSession, fixture_json_dir: Path, deadline: str
) -> None:
    _reload_fixture(session, "paid_credits")
    popup = _ProviderPopup(session, "codex")
    assert "⚡" in popup.panel
    data = json.loads((fixture_json_dir / "paid_credits.json").read_text())
    provider = next(item for item in data["providers"] if item["provider"] == "codex")
    if deadline == "stale_at":
        provider["stale_at"] = data["fetched_at"]
    else:
        provider["display_windows"][0]["reset_at"] = data["fetched_at"]
    session.call("SetStreamStatus", "disconnected", "cached view retained")
    session.call("SetViewForTest", json.dumps(data))
    assert "⚡" not in popup.panel
    assert "paid credits available" not in popup.status
    assert len(popup.windows) == 2


def test_openai_platform_credits(session: GnomeSession) -> None:
    _reload_fixture(session, "openai_platform_credits")
    popup = _ProviderPopup(session, "openai_platform")
    grants = popup.grants
    assert popup.header == "OpenAI API — Available balance: $126"
    assert not popup.balance
    assert "Grant: $100 left" in grants
    assert "Grant: $26 left" in grants
    assert "expires" in grants
    assert "original $100.00" not in popup.details
    assert "original $30.00" in popup.details
    assert string_reply(session.call("GetEmptyLabelText")) == "AI quota"


def test_platform_credits_with_subscription_resets(session: GnomeSession) -> None:
    _reload_fixture(session, "platform_credits_with_subscription_resets")
    claude = _ProviderPopup(session, "claude")
    assert "Available balance: $26" in claude.header
    assert len(claude.windows) == 2

    codex = _ProviderPopup(session, "codex")
    resets = codex.reset
    assert "Banked resets: 3 · next known expiry" in resets
    assert "1 expiry unknown" in resets
    assert "known expiries" in codex.details
    assert len(codex.windows) == 2

    openai = _ProviderPopup(session, "openai_platform")
    grants = openai.grants
    assert "Available balance: $60" in openai.header
    assert not openai.balance
    assert "Grant: $60" in grants
    assert "Jun 1" in grants


def test_wire_fields(session: GnomeSession) -> None:
    _reload_fixture(session, "wire_fields")
    codex = _ProviderPopup(session, "codex")
    windows = codex.windows
    assert len(windows) == 1
    assert windows[0].startswith("visible (5h):")
    assert "45m" in windows[0]
    assert "pp" not in windows[0]
    assert "-55 pp" in codex.details
    assert "internal-only" not in "\n".join(windows)


def test_binding_summary(session: GnomeSession) -> None:
    _reload_fixture(session, "binding_summary")
    assert "+10 pp" in _ProviderPopup(session, "claude").panel
    assert "+18 pp" in _ProviderPopup(session, "codex").panel
    assert "-15 pp" in _ProviderPopup(session, "zai").panel
    assert "Forecasts use average usage since the window started." in _ProviderPopup(session, "claude").details


def test_peak_burn_identifies_affected_models_in_compact_menu(session: GnomeSession) -> None:
    _reload_fixture(session, "zai_peak")
    popup = _ProviderPopup(session, "zai")
    burn = popup.burn
    assert "3x burn until" in burn
    assert "GLM-5.3, GLM-5-Turbo" in burn
    assert "Thu 06:00-10:00" not in burn
    assert "Thu 06:00-10:00" in popup.details
    geometry = session.open_menu()
    try:
        assert geometry[2] <= 480, f"peak scope widens popup: {geometry}"
    finally:
        session.close_menu()


def test_reset_pending(session: GnomeSession) -> None:
    _reload_fixture(session, "reset_pending")
    codex = _ProviderPopup(session, "codex")
    windows = codex.windows
    assert len(windows) == 1
    assert "awaiting update" in windows[0]
    assert "exhausted" not in windows[0]
    panel = codex.panel
    assert panel.startswith("↻?")
    assert "⚡" not in panel
    assert "Reset time passed · awaiting updated usage" in codex.status


def test_stale_observation(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
    _reload_fixture(session, "stale_observation")
    claude = _ProviderPopup(session, "claude")
    status = claude.status
    panel = claude.panel
    windows = claude.windows
    assert "Stale · observation" in status
    assert panel.startswith("~")
    assert len(windows) == 1
    assert "pace:" not in windows[0]

    session.call("SetStreamStatus", "disconnected", "cached view retained")
    disconnected_status = claude.status
    disconnected_panel = claude.panel
    assert "Stale · observation" in disconnected_status
    assert disconnected_panel.startswith("~")
    assert "cached view retained" in string_reply(session.call("GetConnectionDetails"))

    try:
        geometry = session.open_menu()
        capture_menu.capture(geometry, label="stale observation")
    finally:
        session.close_menu()


def test_paid_credits(session: GnomeSession) -> None:
    _reload_fixture(session, "paid_credits")
    codex = _ProviderPopup(session, "codex")
    windows = codex.windows
    assert len(windows) == 2
    assert "Codex — Paid credits:" in codex.header
    assert "Banked resets: 0" in codex.reset
    assert "Included quota exhausted" not in codex.status
    assert "paid credits available" not in codex.status
    assert any("100%" in window for window in windows)
    assert "⚡" in codex.panel


def test_active_extra_usage_falls_back_to_status_without_monthly_spend_row(
    session: GnomeSession, fixture_json_dir: Path
) -> None:
    _reload_fixture(session, "hot")
    data = json.loads((fixture_json_dir / "hot.json").read_text())
    claude = next(provider for provider in data["providers"] if provider["provider"] == "claude")
    claude["monthly_extra_spend"] = None
    session.call("SetViewForTest", json.dumps(data))

    popup = _ProviderPopup(session, "claude")
    assert "Extra usage enabled" in popup.status
    assert "Extra spend" not in popup.text("GetSpendText")
    assert "100%" in " ".join(popup.windows)


def test_observed_account_2026_11_04(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
    _reload_fixture(session, "observed_account_2026_11_04")
    claude = _ProviderPopup(session, "claude")
    codex = _ProviderPopup(session, "codex")
    anthropic = _ProviderPopup(session, "anthropic_platform")
    openai = _ProviderPopup(session, "openai_platform")
    assert len(claude.windows) == 1
    assert len(codex.windows) == 1
    assert "Claude — Available balance:" in claude.header
    assert "Codex — Paid credits:" in codex.header
    assert not claude.balance
    assert not codex.balance
    assert json.loads(string_reply(session.call("GetPanelProviders"))) == ["claude", "codex"]
    openai_grants = openai.grants
    assert len(openai_grants.splitlines()) == 3
    grants = anthropic.grants
    assert len(grants.splitlines()) == 2
    assert re.findall(r"expires ([^·\n]+)", grants) == ["Jul 1, 2027", "Nov 25"]
    assert re.findall(r"expires ([^·\n]+)", openai_grants) == ["Dec 1", "Jan 1, 2027", "Feb 1, 2027"]
    assert "scope:" not in grants
    assert "managed-agents-onprem" in anthropic.details
    assert "Promo grant: $198 left" not in anthropic.details
    assert "Promo grant: original" in anthropic.details
    assert "exact remaining $197.93" in anthropic.details

    geometry = session.open_menu()
    try:
        assert geometry[2] <= 480, f"account popup exceeds compact width: {geometry}"
        session.call("SetDetailsOpen", "anthropic_platform", "true")
        expanded = session.open_menu()
        assert expanded[2] <= 480, f"expanded scope widens account popup: {expanded}"
        capture_menu.capture(expanded, label="Observed account promo details", checkpoint="promo-details")
        session.call("SetDetailsOpen", "anthropic_platform", "false")
    finally:
        session.close_menu()


def test_anthropic_organization_headers_and_group_transitions(session: GnomeSession) -> None:
    popup = _ProviderPopup(session, "anthropic_platform")
    _reload_fixture(session, "platform_credits_with_subscription_resets")
    assert popup.header == "Anthropic API — Shared org"
    (single,) = popup.groups
    assert single["header"] == ""
    assert single["balance"] == "Available balance: $75"
    assert "Grant: $50" in single["grants"]
    assert "Promo grant: $25" in single["grants"]
    assert "scope: Claude API (Sonnet)" in single["grants"]
    assert "Shared org" not in single["grants"]
    assert "Shared org" not in popup.details

    _reload_fixture(session, "anthropic_platform_credits")
    assert popup.header == "Anthropic API"
    main, secondary = popup.groups
    assert main["header"] == "Main org"
    assert secondary["header"] == "Secondary org"
    assert main["balance"] == "Available balance: $123"
    assert secondary["balance"] == "Available balance: $50"
    assert "Grant: $85" in main["grants"]
    assert "Promo grant" in main["grants"]
    assert "scope: Claude API (Sonnet), Claude Code" in main["grants"]
    assert "scope: unscoped" not in main["grants"]
    assert "Main org" not in main["grants"]
    assert "Expired grant" not in main["grants"]
    assert secondary["grants"] == ""

    _reload_fixture(session, "platform_credits_with_subscription_resets")
    assert popup.header == "Anthropic API — Shared org"
    assert len(popup.groups) == 1


def test_anthropic_balance_error_remains_visible_under_its_header(
    session: GnomeSession, fixture_json_dir: Path
) -> None:
    data = json.loads((fixture_json_dir / "platform_credits_with_subscription_resets.json").read_text())
    provider = next(item for item in data["providers"] if item["provider"] == "anthropic_platform")
    provider["balances"][0].update(value="lookup failed", error="example lookup failure")
    session.close_menu()
    session.call("SetViewForTest", json.dumps(data))
    popup = _ProviderPopup(session, "anthropic_platform")
    assert popup.header == "Anthropic API — Shared org"
    assert popup.groups[0]["balance"] == "Available balance: lookup failed"
    details = popup.details
    assert "Available balance: example lookup failure" in details
    assert "Shared org" not in details


def test_render_distinct_prepaid_and_quota_staleness(
    session: GnomeSession, fixture_json_dir: Path, capture_menu: GnomeMenuCapture
) -> None:
    data = json.loads((fixture_json_dir / "claude_prepaid_balance_stale.json").read_text())
    claude = next(provider for provider in data["providers"] if provider["provider"] == "claude")
    claude["stale_since"] = "2026-01-15T11:00:00Z"
    claude["stale_at"] = "2026-01-15T11:10:00Z"
    claude["balances"][0]["stale_since"] = "2026-01-15T10:00:00Z"
    fixture_path = fixture_json_dir / "claude_distinct_staleness.json"
    fixture_path.write_text(json.dumps(data, indent=2) + "\n")
    session.close_menu()
    session.reload("/fixtures/claude_distinct_staleness.json")

    popup = _ProviderPopup(session, "claude")
    assert "Stale · observation 1h old" in popup.status
    assert "Available balance: $123 · stale 2h ago" in popup.balance

    geometry = session.open_menu()
    try:
        positions = json.loads(popup.text("GetRowPositions"))
        assert positions["status"] < positions["balance"] < positions["window"]
        capture_menu.capture(geometry, label="Claude distinct staleness")
    finally:
        session.close_menu()


def test_render_disconnected_stream(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
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


def test_render_disconnected_without_snapshot(session: GnomeSession, capture_menu: GnomeMenuCapture) -> None:
    _reload_fixture(session, _EMPTY_FIXTURE)
    empty_view = json.dumps({"fetched_at": "2026-10-18T12:00:00Z", "providers": []})
    session.call("SetViewForTest", empty_view)
    session.call("SetStreamStatus", "disconnected", "AIQuota daemon is not running")
    session.wait_for_paint()
    geometry = session.open_menu()
    try:
        capture_menu.capture(geometry, label="stream disconnected without snapshot")
    finally:
        session.close_menu()


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
    codex = _ProviderPopup(session, "codex")
    rows = "\n".join(codex.windows)
    assert "exhausts" not in rows
    assert "on pace" not in rows
    assert " pace:" not in codex.details


if __name__ == "__main__":
    pytest_bazel.main()
