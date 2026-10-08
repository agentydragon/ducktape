"""Haku shell renders and interactions, driven through Playwright."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import expect

from util.testing.page_capture import wait_for_stable
from util.testing.viewports import Viewport
from util.testing.visual_capture import VisualHarness, VisualPage

# gazelle:include_dep //util/testing:visual_fixtures
pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.parametrize("color_scheme", ["light", "dark"])]


@asynccontextmanager
async def _fixture(
    visual: VisualHarness, fixture_id: str, color_scheme: Literal["light", "dark"], *, width: int, height: int
) -> AsyncIterator[VisualPage]:
    async with visual.open(
        fixture_id,
        viewport=Viewport(width=width, height=height, device_scale_factor=2),
        color_scheme=color_scheme,
        window_globals={"__FIXTURE__": fixture_id, "__COLOR_SCHEME__": color_scheme},
    ) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.check(context=fixture_id)
        yield view


async def _close_approvals(view: VisualPage) -> None:
    await view.page.locator(".haku-shell-drawer [aria-label='Close approvals']").click()
    await view.page.wait_for_selector(".haku-shell-drawer", state="hidden")
    await _park_pointer(view)


async def _park_pointer(view: VisualPage) -> None:
    await view.check(context="interaction")
    await view.page.mouse.move(0, 0)
    await wait_for_stable(view.page)


async def _approvals_ready(view: VisualPage) -> None:
    # Approval controls arm asynchronously.
    for label in ("Approve", "Deny"):
        await expect(view.page.locator(f"button:has-text('{label}'):disabled")).to_have_count(0)


async def _shell_ready(view: VisualPage) -> None:
    await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()
    await view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")


@pytest.fixture
def console_viewport() -> Viewport:
    return Viewport(width=1200, height=800)


@pytest_asyncio.fixture(loop_scope="session")
async def console_view(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], console_viewport: Viewport
) -> AsyncIterator[VisualPage]:
    async with _fixture(
        visual, "console", color_scheme, width=console_viewport.width, height=console_viewport.height
    ) as view:
        await _shell_ready(view)
        await _approvals_ready(view)
        yield view


@pytest.fixture
def history_fixture() -> str:
    return "history"


@pytest.fixture
def history_viewport() -> Viewport:
    return Viewport(width=1200, height=1500)


@pytest_asyncio.fixture(loop_scope="session")
async def history_view(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], history_fixture: str, history_viewport: Viewport
) -> AsyncIterator[VisualPage]:
    async with _fixture(
        visual, history_fixture, color_scheme, width=history_viewport.width, height=history_viewport.height
    ) as view:
        await _shell_ready(view)
        await _close_approvals(view)
        yield view


async def test_console(console_view: VisualPage, capture_name: str) -> None:
    await _close_approvals(console_view)
    await console_view.capture(capture_name)


@pytest.mark.parametrize(
    "console_viewport", [Viewport(width=1200, height=800), Viewport(width=390, height=760)], ids=["desktop", "mobile"]
)
async def test_console_drawer(console_view: VisualPage, capture_name: str) -> None:
    await console_view.capture(capture_name)


async def test_not_found(visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str) -> None:
    async with _fixture(visual, "not-found", color_scheme, width=900, height=600) as view:
        await view.page.wait_for_selector(":text('Page not found')", state="attached")
        await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()
        await _close_approvals(view)
        await view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
        await view.capture(capture_name)


async def test_approvals_embed(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(visual, "approvals-embed", color_scheme, width=560, height=820) as view:
        await view.page.wait_for_selector("button:has-text('Approve')", state="attached")
        await _approvals_ready(view)
        await view.capture(capture_name)


@pytest.fixture
def settings_viewport() -> Viewport:
    return Viewport(width=1200, height=1000)


@pytest_asyncio.fixture(loop_scope="session")
async def settings_view(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], settings_viewport: Viewport
) -> AsyncIterator[VisualPage]:
    async with _fixture(
        visual, "settings", color_scheme, width=settings_viewport.width, height=settings_viewport.height
    ) as view:
        await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()
        await _close_approvals(view)
        await view.page.wait_for_selector("[aria-label='Loading Agents']", state="hidden")
        yield view


@pytest.mark.parametrize(
    "settings_viewport", [Viewport(width=1200, height=1000), Viewport(width=390, height=760)], ids=["desktop", "mobile"]
)
async def test_settings(settings_view: VisualPage, capture_name: str) -> None:
    await settings_view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
    await settings_view.capture(capture_name)


@pytest.mark.parametrize(
    ("tab", "ready_text"),
    [
        ("Agents", "Public Coder"),
        ("Grants", "Public Coder"),
        ("Notifications", "This browser"),
        ("System", "Mixed revisions"),
    ],
    ids=["agents", "grants", "notifications", "system"],
)
async def test_settings_tab(settings_view: VisualPage, tab: str, ready_text: str, capture_name: str) -> None:
    await settings_view.page.locator(f"[role='tab']:has-text('{tab}')").click()
    await settings_view.page.wait_for_selector(f"[role='tab'][aria-selected='true']:has-text('{tab}')", state="visible")
    await settings_view.page.wait_for_selector(f":text('{ready_text}')", state="visible")
    await _park_pointer(settings_view)
    await settings_view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
    await settings_view.capture(capture_name)


async def test_settings_grants_history(settings_view: VisualPage, capture_name: str) -> None:
    await settings_view.page.locator("[role='tab']:has-text('Grants')").click()
    await settings_view.page.wait_for_selector("[role='tab'][aria-selected='true']:has-text('Grants')", state="visible")
    await _park_pointer(settings_view)
    await settings_view.page.locator(":text('History')").click()
    await settings_view.page.wait_for_selector(
        ":text('Pilot complete; return to standard diagnostics.')", state="visible"
    )
    await _park_pointer(settings_view)
    await settings_view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
    await settings_view.capture(capture_name)


async def test_settings_grants_revoke(settings_view: VisualPage, capture_name: str) -> None:
    await settings_view.page.locator("[role='tab']:has-text('Grants')").click()
    await settings_view.page.wait_for_selector("[role='tab'][aria-selected='true']:has-text('Grants')", state="visible")
    await _park_pointer(settings_view)
    await settings_view.page.locator("button:has-text('Revoke') >> nth=0").click()
    await settings_view.page.wait_for_selector(":text('Confirm')", state="visible")
    await _park_pointer(settings_view)
    await settings_view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
    await settings_view.capture(capture_name)


async def test_agent_enrollment(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(visual, "agent-enrollment", color_scheme, width=1200, height=900) as view:
        await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()
        await _close_approvals(view)
        await view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
        await view.page.wait_for_selector("[aria-label='Loading Agent enrollment']", state="hidden")
        await view.capture(capture_name)


async def test_agent_enrollment_reconnect(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(visual, "agent-enrollment-reconnect", color_scheme, width=1200, height=900) as view:
        await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()
        await _close_approvals(view)
        await view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
        await view.page.wait_for_selector("[aria-label='Loading Agent enrollment']", state="hidden")
        await view.capture(capture_name)


async def test_agent_enrollment_mobile(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(visual, "agent-enrollment", color_scheme, width=390, height=760) as view:
        await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()
        await _close_approvals(view)
        await view.page.wait_for_selector("[aria-label='Syncing']", state="hidden")
        await view.page.wait_for_selector("[aria-label='Loading Agent enrollment']", state="hidden")
        await view.capture(capture_name)


async def test_history(history_view: VisualPage, capture_name: str) -> None:
    view = history_view
    await view.page.locator("[aria-label='Full'] >> nth=0").click()
    await view.page.wait_for_selector("summary:has-text('Metadata')", state="visible")
    await _park_pointer(view)
    await view.page.locator("summary:has-text('Metadata')").click()
    await view.page.wait_for_selector(".haku-shell-disclosure[open] .haku-shell-disclosure-body", state="visible")
    await _park_pointer(view)
    await view.capture(capture_name)


async def test_history_auto_approved(history_view: VisualPage, capture_name: str) -> None:
    view = history_view
    await view.page.locator("[aria-label='Show auto-approved']").click()
    await view.page.wait_for_selector(":text('Auto-approved by unconditional_v1')", state="visible")
    await _park_pointer(view)
    await view.capture(capture_name)


@pytest.mark.parametrize("history_fixture", ["history-paged"])
@pytest.mark.parametrize("history_viewport", [Viewport(width=1200, height=900)], ids=["desktop"])
async def test_history_paged(history_view: VisualPage, capture_name: str) -> None:
    view = history_view
    await view.page.wait_for_selector("button:has-text('Load older calls')", state="attached")
    await view.page.locator(".haku-page-scroll").evaluate("element => { element.scrollTop = element.scrollHeight; }")
    await view.capture(capture_name)


@pytest.mark.parametrize(
    ("fixture_id", "status"),
    [("sync-current", "Up to date"), ("sync-syncing", "Syncing"), ("sync-error", "Sync error")],
    ids=["current", "syncing", "error"],
)
async def test_sync_status(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str, fixture_id: str, status: str
) -> None:
    async with _fixture(visual, fixture_id, color_scheme, width=600, height=420) as view:
        await view.page.get_by_label(status, exact=True).click()
        await view.page.wait_for_selector("[aria-label='Sync status']", state="visible")
        await _park_pointer(view)
        await view.capture(capture_name)


async def test_session_expiring(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(visual, "session-expiring", color_scheme, width=600, height=420) as view:
        await view.page.locator("[aria-label='Session expiring soon']").click()
        await view.page.wait_for_selector("[aria-label='Console session']", state="visible")
        await _park_pointer(view)
        await view.capture(capture_name)


if __name__ == "__main__":
    pytest_bazel.main()
