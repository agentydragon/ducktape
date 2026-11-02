"""Pytest plugin for package-owned visual tests."""

from collections.abc import AsyncIterator
from typing import Literal

import pytest
import pytest_asyncio
from playwright.async_api import Playwright, async_playwright, expect

from util.testing.frontend_visual.capture import HarnessConfig, VisualHarness, VisualPage
from util.testing.frontend_visual.page_capture import WAIT_TIMEOUT_MS
from util.testing.frontend_visual.viewports import Viewport
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def playwright_driver() -> AsyncIterator[Playwright]:
    expect.set_options(timeout=WAIT_TIMEOUT_MS)
    async with async_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def visual(playwright_driver: Playwright) -> VisualHarness:
    return VisualHarness(playwright_driver, HarnessConfig.from_env(), undeclared_outputs_dir())


@pytest.fixture
def viewport() -> Viewport:
    return Viewport()


@pytest.fixture
def color_scheme() -> Literal["light", "dark"]:
    return "light"


@pytest.fixture
def capture_name(request: pytest.FixtureRequest) -> str:
    assert isinstance(request.node, pytest.Item)
    return request.node.name.removeprefix("test_")


@pytest_asyncio.fixture(loop_scope="session")
async def view(
    visual: VisualHarness, viewport: Viewport, color_scheme: Literal["light", "dark"], capture_name: str
) -> AsyncIterator[VisualPage]:
    """Isolated unnamed harness; pytest's case ID names the default screenshot.

    Override/parametrize viewport and color_scheme as ordinary fixtures. Tests still
    configure the app and own readiness, interactions and explicit capture checkpoints.
    Multiple checkpoints must pass distinct names to capture().
    """
    async with visual.open(viewport=viewport, color_scheme=color_scheme, capture_name=capture_name) as page:
        yield page
