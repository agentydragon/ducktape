"""Shared async Playwright pytest fixtures for e2e tests.

Provides per-test `playwright`, `browser` and `page` fixtures on
`playwright.async_api`. Uses hermetic Chromium from @chrome_headless_shell when
CHROMIUM_HEADLESS_SHELL is set (Bazel), falling back to Playwright's default
browser resolution. The browser launches with the shared container-safe flags
(needed on RBE, where there is no user namespace and /dev/shm is tiny); visual
tests that need deterministic rendering build their own context with
`util.testing.frontend_visual.deterministic_browser_context`, which also seeds
Chromium's generic-font profile preferences.

Every fixture is function-scoped: pytest-asyncio runs an async fixture on the
event loop of its own scope, so a session-scoped `playwright` would force every
test that uses it onto the session loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Browser, Page, Playwright, async_playwright

from util.testing.frontend_visual import CONTAINER_BASE_BROWSER_ARGS, chromium_executable


@pytest.fixture
async def playwright() -> AsyncIterator[Playwright]:
    async with async_playwright() as manager:
        yield manager


@pytest.fixture
async def browser(playwright: Playwright) -> AsyncIterator[Browser]:
    async with await playwright.chromium.launch(
        headless=True, executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS
    ) as launched:
        yield launched


@pytest.fixture
async def page(browser: Browser) -> AsyncIterator[Page]:
    async with await browser.new_context() as context:
        yield await context.new_page()
