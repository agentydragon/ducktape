"""Measuring the thread history from a Playwright page: a probe injected before the page loads
(history_probe.js) and an optional CPU throttle, so that timing-dependent behaviour shows up in
one run rather than one in thirty."""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

# How many times slower than the machine the page runs, e.g. `--test_env=AGENTPLANE_CPU_THROTTLE=4`.
_THROTTLE_ENV = "AGENTPLANE_CPU_THROTTLE"


def script_path() -> Path:
    return get_required_path("_main/agentplane/app/testing/history_probe.js")


def cpu_throttle() -> float:
    return float(os.environ.get(_THROTTLE_ENV, "1"))


async def throttle_cpu(context: BrowserContext, page: Page) -> None:
    rate = cpu_throttle()
    if rate > 1:
        session = await context.new_cdp_session(page)
        await session.send("Emulation.setCPUThrottlingRate", {"rate": rate})


async def write_results(page: Page, path: Path) -> None:
    """What the probe saw in each document this page has shown, with the throttle it ran under."""
    try:
        documents: list[dict[str, Any]] | None = await page.evaluate("() => window.__historyProbe?.collect() ?? null")
    except Exception:
        logger.warning("history probe results could not be read from the page", exc_info=True)
        return
    if documents and any(document["framesWithRows"] for document in documents):
        await asyncio.to_thread(path.write_text, json.dumps({"cpu_throttle": cpu_throttle(), "documents": documents}))
