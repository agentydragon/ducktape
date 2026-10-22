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

pytest_plugins = ("util.testing.visual_fixtures",)
pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.parametrize("color_scheme", ["light", "dark"])]


@asynccontextmanager
async def _fixture(
    visual: VisualHarness,
    fixture_id: str,
    color_scheme: Literal["light", "dark"],
    *,
    width: int,
    height: int,
    capture_name: str,
) -> AsyncIterator[VisualPage]:
    async with visual.open(
        fixture_id,
        viewport=Viewport(width=width, height=height, device_scale_factor=2),
        color_scheme=color_scheme,
        window_globals={"__FIXTURE__": fixture_id, "__COLOR_SCHEME__": color_scheme},
        capture_name=capture_name,
    ) as view:
        await expect(view.page.locator("#app > *").first).to_be_attached()
        await view.check(context=fixture_id)
        yield view


async def _park_pointer(view: VisualPage) -> None:
    await view.check(context="interaction")
    await view.page.mouse.move(0, 0)
    await wait_for_stable(view.page)


async def _shell_ready(view: VisualPage) -> None:
    await expect(view.page.frame_locator("iframe[src^='https://haku-ui.test/']").locator("main")).to_be_attached()


@pytest.fixture
def console_viewport() -> Viewport:
    return Viewport(width=1200, height=800)


@pytest_asyncio.fixture(loop_scope="session")
async def console_view(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], console_viewport: Viewport, capture_name: str
) -> AsyncIterator[VisualPage]:
    async with _fixture(
        visual,
        "console",
        color_scheme,
        width=console_viewport.width,
        height=console_viewport.height,
        capture_name=capture_name,
    ) as view:
        await _shell_ready(view)
        yield view


@pytest.mark.parametrize(
    "console_viewport", [Viewport(width=1200, height=800), Viewport(width=390, height=760)], ids=["desktop", "mobile"]
)
async def test_console_responsive(console_view: VisualPage) -> None:
    await console_view.capture()


async def test_capability_request(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(
        visual, "capability-request", color_scheme, width=1200, height=800, capture_name=capture_name
    ) as view:
        await view.page.wait_for_selector(".haku-shell-drawer [aria-label='Capability requests']", state="visible")
        await expect(view.page.locator(".haku-shell-drawer button:has-text('Approve'):disabled")).to_have_count(0)
        await _park_pointer(view)
        await view.capture()

        await view.page.get_by_title("Full", exact=True).click()
        await expect(view.page.get_by_text("Requested", exact=True)).to_be_visible()
        await _park_pointer(view)
        await view.capture(f"capability_request_detailed_{color_scheme}")


async def test_not_found(visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str) -> None:
    async with _fixture(visual, "not-found", color_scheme, width=900, height=600, capture_name=capture_name) as view:
        await view.page.wait_for_selector(":text('Page not found')", state="attached")
        await _shell_ready(view)
        await view.capture()


@pytest.fixture
def settings_viewport() -> Viewport:
    return Viewport(width=1200, height=1000)


@pytest_asyncio.fixture(loop_scope="session")
async def settings_view(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], settings_viewport: Viewport, capture_name: str
) -> AsyncIterator[VisualPage]:
    async with _fixture(
        visual,
        "settings",
        color_scheme,
        width=settings_viewport.width,
        height=settings_viewport.height,
        capture_name=capture_name,
    ) as view:
        await _shell_ready(view)
        await view.page.wait_for_selector("[aria-label='Loading Agents']", state="hidden")
        yield view


@pytest.mark.parametrize(
    "settings_viewport", [Viewport(width=1200, height=1000), Viewport(width=390, height=760)], ids=["desktop", "mobile"]
)
async def test_settings(settings_view: VisualPage) -> None:
    await settings_view.capture()


@pytest.mark.parametrize(
    ("tab", "ready_text"), [("Agents", "Public Coder"), ("System", "Mixed revisions")], ids=["agents", "system"]
)
async def test_settings_tab(settings_view: VisualPage, tab: str, ready_text: str) -> None:
    await settings_view.page.locator(f"[role='tab']:has-text('{tab}')").click()
    await settings_view.page.wait_for_selector(f"[role='tab'][aria-selected='true']:has-text('{tab}')", state="visible")
    await settings_view.page.wait_for_selector(f":text('{ready_text}')", state="visible")
    await _park_pointer(settings_view)
    await settings_view.capture()


async def test_session_expiring(
    visual: VisualHarness, color_scheme: Literal["light", "dark"], capture_name: str
) -> None:
    async with _fixture(
        visual, "session-expiring", color_scheme, width=600, height=420, capture_name=capture_name
    ) as view:
        await view.page.locator("[aria-label='Session expiring soon']").click()
        await view.page.wait_for_selector("[aria-label='Console session']", state="visible")
        await _park_pointer(view)
        await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
