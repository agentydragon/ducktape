"""Shared Playwright pytest fixtures for e2e tests.

Provides session-scoped Playwright manager, per-test browser and page fixtures.
Uses hermetic Chromium from @playwright_browsers when CHROMIUM_HEADLESS_SHELL is
set (Bazel), falling back to Playwright's default browser resolution. The
browser launches with the shared container-safe flags (needed on RBE, where
there is no user namespace and /dev/shm is tiny); visual tests that need
deterministic rendering override `browser` with
`util.testing.frontend_visual.launch_deterministic_browser`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

from util.testing.frontend_visual import CONTAINER_BASE_BROWSER_ARGS, chromium_executable

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright


@pytest.fixture(scope="session")
def playwright_sync() -> Iterator[Playwright]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as manager:
        yield manager


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    browser = playwright_sync.chromium.launch(
        headless=True, executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS
    )
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context()
    pg = context.new_page()
    try:
        yield pg
    finally:
        try:
            pg.close()
        finally:
            context.close()
