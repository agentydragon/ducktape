from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest

from util.playwright import browser, page, playwright_sync
from x.agent_server.agent_types import AgentID
from x.agent_server.server.app import create_app
from x.agent_server.testing.helpers import ServerHandle, start_uvicorn_app

# Auto-apply e2e marker to all tests in this directory
pytestmark = [pytest.mark.e2e]


def pytest_configure(config: pytest.Config) -> None:
    """Configure pytest-asyncio auto mode for e2e tests."""
    config.option.asyncio_mode = "auto"
    config.option.asyncio_default_fixture_loop_scope = "function"


if TYPE_CHECKING:
    from playwright.sync_api import Page


@pytest.fixture
def run_server(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Start the UI server (FastAPI+WS) on a background thread; return ServerHandle.

    Uses a per-test SQLite DB via ADGN_AGENT_DB_PATH; serves built static assets.
    Shared by e2e UI tests to avoid duplication.
    """

    def _start(client_factory: Callable[[str], Any] | None = None) -> ServerHandle:
        db_path = tmp_path / "agent.sqlite"
        monkeypatch.setenv("ADGN_AGENT_DB_PATH", str(db_path))
        app = create_app(require_static_assets=True)
        return start_uvicorn_app(app)

    return _start


## patch_model fixture retired; tests pass a client_factory to run_server instead


# ---- E2E helper class ----


class E2EPageHelper:
    """Helper class wrapping Playwright Page with agent-specific methods."""

    def __init__(self, page: Page) -> None:
        self.page = page
        # Hook console logging for debugging WS/UI issues
        page.on("console", lambda msg: print(f"[browser console] {msg.type}: {msg.text}"))

    def goto(self, url: str) -> None:
        """Navigate to URL and wait for page load."""
        self.page.goto(url)

    def goto_agent(self, base_url: str, agent_id: AgentID) -> None:
        """Navigate to a specific agent's page and wait for WS connection."""
        self.page.goto(f"{base_url}/?agent_id={agent_id}")
        self.wait_for_ws_connected()

    def wait_for_ws_connected(self, timeout: int = 10000) -> None:
        """Wait for WebSocket connection indicator."""
        self.page.locator(".ws .dot.on").wait_for(timeout=timeout)

    def reload_and_reconnect(self, timeout: int = 10000) -> None:
        """Reload page and wait for WS to reconnect."""
        self.page.reload()
        self.wait_for_ws_connected(timeout=timeout)

    def extract_agent_id_from_url(self) -> AgentID:
        """Extract agent_id query parameter from current URL."""
        qs = parse_qs(urlparse(self.page.url).query)
        agent_id: str | None = qs.get("agent_id", [None])[0]
        if not agent_id:
            raise ValueError("No agent_id in URL")
        return agent_id

    def create_agent_via_ui(self) -> None:
        """Create a new agent via the UI by clicking through the create flow."""
        create_btn = self.page.get_by_role("button", name="Create new agent")
        create_btn.wait_for(timeout=10000)
        create_btn.click()

        create_modal_btn = self.page.get_by_role("button", name="Create")
        create_modal_btn.wait_for(timeout=5000)
        create_modal_btn.click()

        # Wait for WS connected after agent is created
        self.wait_for_ws_connected()

    def send_prompt(self, text: str) -> None:
        """Type a prompt and click Send."""
        self.page.locator('textarea[placeholder^="Type a prompt"]').fill(text)
        self.page.get_by_role("button", name="Send").click()

    def wait_for_message(self, text: str, timeout: int = 5000) -> None:
        """Wait for a message with specific text to appear."""
        self.page.locator(".messages .msg .text", has_text=text).wait_for(timeout=timeout)

    def wait_for_text(self, text: str, timeout: int = 10000) -> None:
        """Wait for any text to appear on the page."""
        self.page.get_by_text(text).wait_for(timeout=timeout)

    def click_approve(self) -> None:
        """Click the first Approve button."""
        self.page.get_by_role("button", name="Approve").first.click()

    def click_reject(self) -> None:
        """Click the first Reject button."""
        self.page.get_by_role("button", name="Reject").first.click()

    def wait_and_click_abort(self, timeout: int = 5000) -> None:
        """Wait for Abort button to appear and click it."""
        abort_btn = self.page.get_by_role("button", name="Abort")
        abort_btn.wait_for(timeout=timeout)
        abort_btn.click()

    async def create_proposal_via_mcp(self, policy_content: str) -> str:
        """Create a policy proposal via the MCP client (mimics agent behavior).

        Returns the proposal ID.
        """
        return await self.page.evaluate(  # type: ignore[no-any-return]
            """async (content) => {
                const { currentClient } = await import('/src/features/chat/stores.ts');
                const client = currentClient.get();
                if (!client) throw new Error('No MCP client available');
                const res = await client.callTool('policy_proposer.create_proposal', { content });
                return res.structuredContent?.id || res.content[0]?.text;
            }""",
            policy_content,
        )


@pytest.fixture
def e2e_page(page: Page) -> E2EPageHelper:
    """Provide an E2E page helper wrapping the Playwright page."""
    return E2EPageHelper(page)
