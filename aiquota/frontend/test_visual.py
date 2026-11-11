"""Dashboard fixture screenshots, render health, and focused behavior checks."""

import json
import os
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import expect
from python.runfiles import Runfiles

from aiquota.testing.quota_fixtures import meter_quota_view
from util.testing.frontend_visual.capture import VisualHarness, VisualPage
from util.testing.frontend_visual.viewports import Viewport

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = pytest.mark.asyncio(loop_scope="session")

_FIXTURES = json.loads((Runfiles.CreateOrRaise().root() / os.environ["FIXTURE_CATALOG"]).read_bytes())
_SCENES = tuple(_FIXTURES)
if not _SCENES:
    raise ValueError("the dashboard fixture catalog is empty")

_DESKTOP_VIEWPORT = Viewport(width=1200, height=900, device_scale_factor=2)
_NARROW_VIEWPORT = Viewport(width=420, height=900, device_scale_factor=2)
_DESKTOP_THEME_VIEWPORTS = [pytest.param(scheme, _DESKTOP_VIEWPORT, id=scheme) for scheme in ("light", "dark")]
_OBSERVED_ACCOUNT_THEME_VIEWPORTS = [
    *_DESKTOP_THEME_VIEWPORTS,
    pytest.param("dark", _NARROW_VIEWPORT, id="dark-narrow"),
]


@pytest_asyncio.fixture(loop_scope="session")
async def view(
    visual: VisualHarness, scene: str, color_scheme: Literal["light", "dark"], viewport: Viewport
) -> AsyncIterator[VisualPage]:
    async with visual.open(query={"scene": scene}, viewport=viewport, color_scheme=color_scheme) as view:
        yield view


@pytest.mark.parametrize(
    ("scene", "color_scheme", "viewport"),
    [
        pytest.param(scene, scheme, _DESKTOP_VIEWPORT, id=f"{scene}-{scheme}")
        for scene in _SCENES
        for scheme in ("light", "dark")
    ]
    + [
        pytest.param(scene, "dark", _NARROW_VIEWPORT, id=f"{scene}-dark-narrow")
        for scene in ("hot", "paid_credits", "observed_account_2026_11_04", "banked_resets_ordinary")
    ],
)
async def test_dashboard(view: VisualPage) -> None:
    await expect(view.page.locator(".aiquota-card").first).to_be_attached()
    await expect(view.page.get_by_role("status")).to_have_text("Live updates connected")
    await view.capture(target=view.page.locator("#app"))


@pytest.mark.parametrize(
    ("scene", "default_visible", "known"),
    [
        ("banked_resets_ordinary", False, True),
        ("banked_resets_exhausted", True, True),
        ("banked_resets_pending", False, True),
        ("banked_resets_unknown", False, False),
    ],
)
@pytest.mark.parametrize(
    ("color_scheme", "viewport"), [*_DESKTOP_THEME_VIEWPORTS, pytest.param("dark", _NARROW_VIEWPORT, id="dark-narrow")]
)
async def test_banked_reset_visibility(view: VisualPage, default_visible: bool, known: bool) -> None:
    card = view.page.locator('.aiquota-card[aria-label="Codex quota"]')
    await expect(card.locator(".aiquota-strip-line")).to_have_count(int(default_visible))
    details = card.locator(".aiquota-strip details")
    await expect(details).to_have_count(int(known))
    if known:
        await details.get_by_text("Details", exact=True).click()
        await expect(details.get_by_text("Banked resets: 0", exact=True)).to_be_visible()
        await view.capture("reset-details", target=view.page.locator("#app"))


@pytest.mark.parametrize("scene", ["reset_credit_expiry_unavailable"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_reset_credit_expiry_lookup_unavailable(view: VisualPage) -> None:
    await expect(view.page.locator(".aiquota-strip-resets .aiquota-strip-line")).to_have_text(
        "Banked resets: 2 · expiry lookup failed"
    )


@pytest.mark.parametrize("scene", ["claude_reset_grants"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_claude_reset_grants(view: VisualPage) -> None:
    card = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(card.get_by_text("Fable resets", exact=True)).to_be_visible()
    await expect(card.get_by_text("2 remaining", exact=True)).to_be_visible()
    await expect(card.get_by_text("Full resets", exact=True)).to_be_visible()
    await expect(card.get_by_text("paused", exact=False)).to_be_visible()


@pytest.mark.parametrize("scene", ["zai_peak"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_peak_burn_identifies_affected_models(view: VisualPage) -> None:
    card = view.page.locator('.aiquota-card[aria-label="z.ai quota"]')
    await expect(card.get_by_text("3x burn until", exact=False)).to_be_visible()
    await expect(card.get_by_text("GLM-5.3, GLM-5-Turbo", exact=False)).to_be_visible()


@pytest.mark.parametrize("scene", ["openai_platform_credits"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_openai_platform_credits(view: VisualPage) -> None:
    card = view.page.locator('.aiquota-card[aria-label="OpenAI API quota"]')
    await expect(card.get_by_role("heading", name="OpenAI API")).to_be_visible()
    await expect(card.get_by_text("Available balance", exact=True)).to_be_visible()
    await expect(card.get_by_text("$126", exact=True)).to_be_visible()
    await expect(card.get_by_text("$100 remaining", exact=True)).to_be_visible()
    await expect(card.get_by_text("$26 remaining", exact=True)).to_be_visible()


@pytest.mark.parametrize(
    ("scene", "expected_balance"),
    [
        pytest.param("claude_prepaid_balance", "$123", id="available"),
        pytest.param("claude_prepaid_balance_unavailable", "unavailable", id="unavailable"),
    ],
)
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_claude_prepaid_balance_states(view: VisualPage, expected_balance: str) -> None:
    card = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(card.get_by_role("heading", name="Claude")).to_be_visible()
    await expect(card.get_by_text("Available balance", exact=True)).to_be_visible()
    await expect(card.get_by_text(expected_balance, exact=True)).to_be_visible()


@pytest.mark.parametrize("scene", ["claude_prepaid_balance_stale"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_claude_prepaid_balance_stale(view: VisualPage) -> None:
    card = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(card.get_by_role("heading", name="Claude")).to_be_visible()
    await expect(card.get_by_text("Balance stale · 1h", exact=True)).to_be_visible()
    await expect(card.locator(".aiquota-badge-stale")).to_have_count(0)


@pytest.mark.parametrize("scene", ["stale_fallback"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_stale_fallback(view: VisualPage) -> None:
    claude = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(claude.get_by_role("heading")).to_have_text("Claude")
    await expect(claude.get_by_text("Data stale · 1d", exact=True)).to_be_visible()


@pytest.mark.parametrize("scene", ["extra_disabled_positive", "extra_spend_fallback"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_monthly_spend_is_history(view: VisualPage, scene: str) -> None:
    card = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(card.get_by_text("Extra spend this month: $26 of $100 limit", exact=True)).to_be_visible()
    await expect(card.get_by_text("extra usage enabled", exact=False)).to_have_count(0)
    if scene == "extra_spend_fallback":
        await expect(card.locator(".aiquota-badge-stale")).to_be_visible()
        await expect(card.locator(".aiquota-badge-error")).to_be_visible()


@pytest.mark.parametrize("scene", ["multi_window_reset_pending", "stale_paid_signals"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_uncertain_paid_signals(view: VisualPage, scene: str) -> None:
    for label in ("Claude", "Codex"):
        card = view.page.locator(f'.aiquota-card[aria-label="{label} quota"]')
        await expect(card.get_by_text("extra usage enabled", exact=False)).to_have_count(0)
        await expect(card.get_by_text("paid credits available", exact=False)).to_have_count(0)
        if scene == "multi_window_reset_pending":
            await expect(card.get_by_text("Reset time passed · awaiting updated usage", exact=True)).to_be_visible()
            await expect(card.locator(".aiquota-window")).to_have_count(2)
        else:
            await expect(card.locator(".aiquota-badge-stale")).to_be_visible()


@pytest.mark.parametrize("scene", ["anthropic_platform_credits"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_anthropic_platform_credits(view: VisualPage) -> None:
    card = view.page.locator('.aiquota-card[aria-label="Anthropic API quota"]')
    await expect(card.get_by_role("heading", name="Anthropic API")).to_be_visible()
    await expect(card.get_by_text("Main org", exact=True)).to_be_visible()
    await expect(card.get_by_text("$123", exact=True)).to_be_visible()
    await expect(card.get_by_text("Promo grant", exact=True).first).to_be_visible()
    await expect(card.get_by_text("$25 remaining", exact=True)).to_be_visible()


@pytest.mark.parametrize("scene", ["platform_credits_with_subscription_resets"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_platform_credits_with_subscription_resets(view: VisualPage) -> None:
    claude = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(claude.get_by_role("heading")).to_have_text("Claude")
    await expect(claude.get_by_text("$26", exact=True)).to_be_visible()
    await expect(claude.locator(".aiquota-window")).to_have_count(2)
    codex = view.page.locator('.aiquota-card[aria-label="Codex quota"]')
    await expect(codex.get_by_role("heading")).to_contain_text("Codex")
    await expect(codex.locator(".aiquota-strip-resets .aiquota-strip-line")).to_contain_text(
        "Banked resets: 3 · next known expiry"
    )
    await expect(codex.locator(".aiquota-window")).to_have_count(2)
    openai = view.page.locator('.aiquota-card[aria-label="OpenAI API quota"]')
    await expect(openai.get_by_role("heading", name="OpenAI API")).to_be_visible()
    await expect(openai.get_by_text("$60", exact=True)).to_be_visible()
    anthropic = view.page.locator('.aiquota-card[aria-label="Anthropic API quota"]')
    await expect(anthropic.get_by_text("Shared org", exact=True)).to_be_visible()
    await expect(anthropic.get_by_text("$75", exact=True)).to_be_visible()
    await expect(anthropic.get_by_text("$25 remaining", exact=True)).to_be_visible()


@pytest.mark.parametrize("scene", ["reset_pending"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_reset_pending(view: VisualPage) -> None:
    codex = view.page.locator('.aiquota-card[aria-label="Codex quota"]')
    await expect(codex.get_by_text("Reset time passed · awaiting updated usage", exact=True)).to_be_visible()
    await expect(codex.get_by_text("Awaiting updated usage", exact=True)).to_be_visible()
    await expect(codex.get_by_text("Numeric pace", exact=False)).to_have_count(0)


@pytest.mark.parametrize("scene", ["stale_observation"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _DESKTOP_THEME_VIEWPORTS)
async def test_stale_observation(view: VisualPage) -> None:
    claude = view.page.locator('.aiquota-card[aria-label="Claude quota"]')
    await expect(claude.get_by_text("Data stale", exact=False)).to_be_visible()
    await expect(claude.locator(".aiquota-error-details")).to_have_count(0)


@pytest.mark.parametrize("scene", ["observed_account_2026_11_04"])
@pytest.mark.parametrize(("color_scheme", "viewport"), _OBSERVED_ACCOUNT_THEME_VIEWPORTS)
async def test_observed_account_expiries_and_promo_details(view: VisualPage) -> None:
    await expect(view.page.locator(".aiquota-window")).to_have_count(2)
    await expect(view.page.get_by_text("Grant details", exact=True)).to_have_count(5)
    card = view.page.locator('.aiquota-card[aria-label="Anthropic API quota"]')
    await expect(card.get_by_text("Expires Nov 25", exact=True)).to_be_visible()
    await expect(card.get_by_text("Expires Jul 1, 2027", exact=True)).to_be_visible()
    openai = view.page.locator('.aiquota-card[aria-label="OpenAI API quota"]')
    for date in ("Dec 1", "Jan 1, 2027", "Feb 1, 2027"):
        await expect(openai.get_by_text(f"Expires {date}", exact=True)).to_be_visible()
    await expect(card.locator(".aiquota-reading-note").filter(has_text="Scope:")).to_have_count(0)
    promo = card.locator(".aiquota-reading-row").filter(has=view.page.get_by_text("Promo grant", exact=True))
    await promo.get_by_text("Grant details", exact=True).click()
    await expect(promo.get_by_text("managed-agents-onprem", exact=False)).to_be_visible()
    await view.capture("promo-details", target=view.page.locator("#app"))
    await promo.get_by_text("Grant details", exact=True).click()


async def test_system_color_preference_changes(visual: VisualHarness) -> None:
    async with visual.open(query={"scene": "hot"}, color_scheme="light") as view:
        page = view.page
        await expect(page.locator("html")).to_have_attribute("data-mantine-color-scheme", "light")
        card = page.locator(".aiquota-card").first
        await expect(card).to_be_visible()
        light_background = await card.evaluate("element => getComputedStyle(element).backgroundColor")
        await page.emulate_media(color_scheme="dark")
        await expect(page.locator("html")).to_have_attribute("data-mantine-color-scheme", "dark")
        dark_background = await card.evaluate("element => getComputedStyle(element).backgroundColor")
        assert light_background != dark_background


async def test_meter_accessibility(visual: VisualHarness) -> None:
    async with visual.open(
        query={"scene": "binding_summary", "live": "1"},
        frozen_now_ms=int(datetime.fromisoformat(_FIXTURES["binding_summary"]["fetched_at"]).timestamp() * 1000),
    ) as view:
        for usage, displayed, value in [(-5, -5, 0), (0, 0, 0), (99.9, 99, 99), (100, 100, 100), (105, 105, 100)]:
            await view.page.evaluate(
                "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
                meter_quota_view(usage).model_dump(mode="json"),
            )
            card = view.page.get_by_label("Codex quota", exact=True)
            meter = card.get_by_role("meter", name="5h quota usage", exact=True)
            await expect(meter).to_have_attribute("aria-valuenow", str(value))
            await expect(meter).to_have_attribute("aria-valuemin", "0")
            await expect(meter).to_have_attribute("aria-valuemax", "100")
            await expect(meter).to_have_attribute("aria-valuetext", f"{displayed}% used, 50% of the window elapsed")
            await expect(meter).to_have_accessible_name("5h quota usage")
            await expect(card.locator(".aiquota-window-used").first).to_have_text(f"{displayed}%")
        await view.capture("meter-over-quota", target=view.page.locator("#app"))


async def test_live_subscription(visual: VisualHarness) -> None:
    async with visual.open(
        query={"scene": "hot", "live": "1"},
        frozen_now_ms=int(datetime.fromisoformat(_FIXTURES["hot"]["fetched_at"]).timestamp() * 1000),
    ) as view:
        page = view.page
        await expect(page.locator("html")).to_have_attribute("data-quota-stream", "connected")
        await expect(page.get_by_text("Loading quota data…")).to_be_visible()
        await expect(page.get_by_role("status")).to_contain_text("Connecting")
        await expect(page.get_by_role("button", name="Reconnect")).to_be_disabled()
        await expect(page.locator("html")).to_have_attribute("data-quota-stream", "connected")
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["hot"],
        )
        await expect(page.locator(".aiquota-card").first).to_be_visible()
        await expect(page.get_by_role("button", name="Reconnect")).to_be_enabled()
        await expect(page.get_by_role("status")).to_have_text("Live updates connected")
        await page.evaluate("window.dispatchEvent(new Event('quota-fixture-error'))")
        await expect(page.get_by_text("Live updates are interrupted; the browser is retrying.")).to_be_visible()
        await expect(page.get_by_role("status")).to_have_text("Live updates unavailable")
        await expect(page.locator(".aiquota-card").first).to_be_visible()
        await view.capture("live-reconnecting", target=page.locator("#app"))
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["paid_credits"],
        )
        await expect(page.locator(".aiquota-notice")).to_have_count(0)
        await expect(page.get_by_text("Paid credits", exact=True).first).to_be_visible()
        await view.capture("live-recovered", target=page.locator("#app"))
        await page.get_by_role("button", name="Reconnect").click()
        await expect(page.get_by_role("button", name="Reconnect")).to_be_disabled()
        await expect(page.locator("html")).to_have_attribute("data-quota-stream", "connected")
        await page.evaluate(
            "data => window.dispatchEvent(new MessageEvent('quota-fixture-view', {data: JSON.stringify(data)}))",
            _FIXTURES["hot"],
        )
        await expect(page.get_by_role("button", name="Reconnect")).to_be_enabled()
        await expect(page.get_by_role("status")).to_have_text("Live updates connected")


if __name__ == "__main__":
    pytest_bazel.main()
