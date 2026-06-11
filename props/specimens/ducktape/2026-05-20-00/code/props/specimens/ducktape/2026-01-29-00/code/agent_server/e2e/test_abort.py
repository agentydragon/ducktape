from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
import pytest_bazel

from openai_utils.testing.openai_mock import make_mock

pytestmark = pytest.mark.usefixtures()

# Skip if Playwright is not installed
playwright = pytest.importorskip("playwright.sync_api")

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = playwright.Page


"""E2E Abort test. Shared fixtures are provided in tests/agent/e2e/conftest.py."""


@pytest.mark.timeout(10)
def test_ui_abort_sampling(e2e_page, run_server, responses_factory):
    """Start a long sampling call, click Abort in UI, and verify the run stops.

    - FE: exercise Abort button visibility/interaction
    - BE: exercise WS abort → session.cancel_active_run → sampling cancellation
    """

    state = {"i": 0}

    async def responses_create(_req):
        # Simulate a long-running first sampling call that should be cancelled by Abort
        state["i"] += 1
        if state["i"] == 1:
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                # Propagate cancellation so the agent treats this as aborted
                raise
            # If not cancelled (unexpected), return a message
            return responses_factory.make_assistant_message("too-late")
        return responses_factory.make_assistant_message("done")

    s = run_server(lambda model: make_mock(responses_create))

    e2e_page.goto(s.base_url)
    e2e_page.create_agent_via_ui()

    # Send a prompt to kick off sampling; Abort button should appear while running
    e2e_page.send_prompt("start long task")

    # Wait for Abort button to become visible and click it
    e2e_page.wait_and_click_abort()

    # After abort, the UI should unblock; type a new draft and expect Send to enable
    e2e_page.page.wait_for_timeout(200)  # brief yield to allow UI update
    e2e_page.page.locator('textarea[placeholder^="Type a prompt"]').fill("draft after abort")
    e2e_page.page.locator('button:has-text("Send"):not([disabled])').wait_for(timeout=5000)

    # Ensure the long-running message was not rendered
    expect_none = e2e_page.page.locator(".messages .msg .text", has_text="too-late")
    assert expect_none.count() == 0

    s.stop()


if __name__ == "__main__":
    pytest_bazel.main()
