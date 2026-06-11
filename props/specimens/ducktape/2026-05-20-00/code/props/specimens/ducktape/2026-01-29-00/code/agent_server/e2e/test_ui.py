from __future__ import annotations

import pytest
import pytest_bazel

from agent_core_testing.responses import DecoratorMock
from agent_server.mcp.ui.server import EndTurnInput, SendMessageInput
from mcp_infra.constants import UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function

pytestmark = pytest.mark.usefixtures()

# Skip if Playwright is not installed
playwright = pytest.importorskip("playwright.sync_api")

"""E2E UI tests. Shared fixtures are provided in tests/agent/e2e/conftest.py."""


class UiMock(DecoratorMock):
    """Mock with UI server helpers."""

    def send_message(self, content: str):
        """Create send_message tool call."""
        return self.tool_call(build_mcp_function(UI_MOUNT_PREFIX, "send_message"), SendMessageInput(content=content))

    def end_turn(self):
        """Create end_turn tool call."""
        return self.tool_call(build_mcp_function(UI_MOUNT_PREFIX, "end_turn"), EndTurnInput())


def test_ui_create_chat_and_restore(e2e_page, run_server):
    """Create agent via UI, send two prompts, restart server, and verify hydration in snapshot.

    - FE: exercise Svelte UI flows (agent creation, chatting, rendering messages)
    - BE: exercise FastAPI HTTP+WS, agent container, persistence + restore
    - LLM: patched to deterministic tool-calls that emit ui messages
    """

    # Program two turns: **r1**, end; then **r2**, end
    @UiMock.mock()
    def mock(m: UiMock):
        yield
        yield m.send_message("**r1**")
        yield m.end_turn()
        yield m.send_message("**r2**")
        yield m.end_turn()

    # Start server instance A
    s1 = run_server(lambda model: mock)

    # 1) Open UI and create agent via the UI
    e2e_page.goto(s1.base_url)
    e2e_page.create_agent_via_ui()

    # Extract agent_id from URL after agent is created
    e2e_page.page.wait_for_url("**/?(agent_id=*", timeout=10000)
    agent_id = e2e_page.extract_agent_id_from_url()

    # 2) Send first prompt and expect Assistant message r1
    e2e_page.send_prompt("hi")
    e2e_page.wait_for_message("r1")

    # 3) Send second prompt and expect Assistant message r2
    e2e_page.send_prompt("again")
    e2e_page.wait_for_message("r2")

    # Stop server A
    s1.stop()

    # Start server instance B (same DB)
    s2 = run_server(lambda model: mock)

    # 4) Re-open UI on the same agent id, wait for WS connected (dot), verify hydration
    e2e_page.goto_agent(s2.base_url, agent_id)
    e2e_page.wait_for_message("r1")
    e2e_page.wait_for_message("r2")

    # Cleanup
    s2.stop()


if __name__ == "__main__":
    pytest_bazel.main()
