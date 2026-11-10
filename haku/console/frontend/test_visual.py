"""Haku shell renders and interactions, driven through Playwright."""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal

import pytest
import pytest_asyncio
import pytest_bazel
from playwright.async_api import expect

from util.testing.frontend_visual.capture import VisualHarness, VisualPage
from util.testing.frontend_visual.page_capture import wait_for_stable
from util.testing.frontend_visual.viewports import Viewport

pytest_plugins = ("util.testing.frontend_visual.fixtures",)
pytestmark = [pytest.mark.asyncio(loop_scope="session"), pytest.mark.parametrize("color_scheme", ["light", "dark"])]


type OpenFixture = Callable[[str, Viewport], AbstractAsyncContextManager[VisualPage]]


@pytest.fixture
def open_fixture(visual: VisualHarness, color_scheme: Literal["light", "dark"]) -> OpenFixture:
    @asynccontextmanager
    async def open_scene(fixture_id: str, viewport: Viewport) -> AsyncIterator[VisualPage]:
        async with visual.open(
            viewport=Viewport(width=viewport.width, height=viewport.height, device_scale_factor=2),
            color_scheme=color_scheme,
            window_globals={"__FIXTURE__": fixture_id, "__COLOR_SCHEME__": color_scheme},
        ) as view:
            await expect(view.page.locator("#app > *").first).to_be_attached()
            await view.check(context=fixture_id)
            yield view

    return open_scene


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
async def console_view(open_fixture: OpenFixture, console_viewport: Viewport) -> AsyncIterator[VisualPage]:
    async with open_fixture("console", console_viewport) as view:
        await _shell_ready(view)
        yield view


@pytest.mark.parametrize(
    "console_viewport", [Viewport(width=1200, height=800), Viewport(width=390, height=760)], ids=["desktop", "mobile"]
)
async def test_console_responsive(console_view: VisualPage) -> None:
    await console_view.capture()


async def test_capability_request(open_fixture: OpenFixture) -> None:
    async with open_fixture("capability-request", Viewport(width=1200, height=800)) as view:
        await view.page.wait_for_selector(".haku-shell-drawer [aria-label='Capability requests']", state="visible")
        await expect(view.page.locator(".haku-shell-drawer button:has-text('Approve'):disabled")).to_have_count(0)
        await _park_pointer(view)
        await view.capture()


async def test_not_found(open_fixture: OpenFixture) -> None:
    async with open_fixture("not-found", Viewport(width=900, height=600)) as view:
        await view.page.wait_for_selector(":text('Page not found')", state="attached")
        await _shell_ready(view)
        await view.capture()


@pytest.fixture
def settings_viewport() -> Viewport:
    return Viewport(width=1200, height=1000)


@pytest_asyncio.fixture(loop_scope="session")
async def settings_view(open_fixture: OpenFixture, settings_viewport: Viewport) -> AsyncIterator[VisualPage]:
    async with open_fixture("settings", settings_viewport) as view:
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


async def test_session_expiring(open_fixture: OpenFixture) -> None:
    async with open_fixture("session-expiring", Viewport(width=600, height=420)) as view:
        await view.page.locator("[aria-label='Session expiring soon']").click()
        await view.page.wait_for_selector("[aria-label='Console session']", state="visible")
        await _park_pointer(view)
        await view.capture()


if __name__ == "__main__":
    pytest_bazel.main()
