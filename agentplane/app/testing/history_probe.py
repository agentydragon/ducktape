"""Opt-in per-frame thread-history probe and optional browser CPU throttle.

Set AGENTPLANE_HISTORY_PROBE=1 to collect expensive layout diagnostics; the lightweight
User Timing marks are collected independently by thread_view_marks.js.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page
from python.runfiles import Runfiles

logger = logging.getLogger(__name__)

# How many times slower than the machine the page runs, e.g. `--test_env=AGENTPLANE_CPU_THROTTLE=4`.
_THROTTLE_ENV = "AGENTPLANE_CPU_THROTTLE"


def enabled() -> bool:
    """Collect expensive per-frame diagnostics only for explicit investigations."""
    return os.environ.get("AGENTPLANE_HISTORY_PROBE") == "1"


def script_path() -> Path:
    return Runfiles.CreateOrRaise().root() / "ducktape/agentplane/app/testing/history_probe.js"


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
