"""Shared Playwright pytest fixtures for e2e tests.

Provides session-scoped Playwright manager, per-test browser and page fixtures.
Uses hermetic Chromium from @playwright_browsers when CHROMIUM_HEADLESS_SHELL is
set (Bazel), falling back to Playwright's default browser resolution.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright


@pytest.fixture(scope="session")
def playwright_sync() -> Iterator[Playwright]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as manager:
        yield manager


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    browser = playwright_sync.chromium.launch(headless=True, executable_path=executable)
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
