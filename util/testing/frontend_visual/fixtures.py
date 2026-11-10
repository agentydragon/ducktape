"""Pytest plugin for package-owned visual tests."""

from collections.abc import AsyncIterator
from typing import Literal

import pytest
import pytest_asyncio
from playwright.async_api import Playwright, async_playwright, expect

from devinfra.testing.output_writer import BoundTestOutputs
from devinfra.testing.pytest_outputs import test_outputs
from util.testing.frontend_visual.capture import HarnessConfig, VisualHarness, VisualPage
from util.testing.frontend_visual.page_capture import WAIT_TIMEOUT_MS
from util.testing.frontend_visual.viewports import Viewport

__all__ = ["test_outputs"]


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def playwright_driver() -> AsyncIterator[Playwright]:
    expect.set_options(timeout=WAIT_TIMEOUT_MS)
    async with async_playwright() as playwright:
        yield playwright


@pytest.fixture
def visual(playwright_driver: Playwright, test_outputs: BoundTestOutputs) -> VisualHarness:
    return VisualHarness(playwright_driver, HarnessConfig.from_env(), test_outputs)


@pytest.fixture
def viewport() -> Viewport:
    return Viewport()


@pytest.fixture
def color_scheme() -> Literal["light", "dark"]:
    return "light"


@pytest_asyncio.fixture(loop_scope="session")
async def view(
    visual: VisualHarness, viewport: Viewport, color_scheme: Literal["light", "dark"]
) -> AsyncIterator[VisualPage]:
    """Isolated unnamed harness; pytest's case ID names the default screenshot.

    Override/parametrize viewport and color_scheme as ordinary fixtures. Tests still
    configure the app and own readiness, interactions and explicit capture checkpoints.
    Multiple checkpoints must pass distinct names to capture().
    """
    async with visual.open(viewport=viewport, color_scheme=color_scheme) as page:
        yield page
