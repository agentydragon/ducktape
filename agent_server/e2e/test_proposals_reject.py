from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_bazel

from agent_core_testing.responses import DecoratorMock
from agent_server.mcp.approval_policy.engine import CreateProposalArgs
from agent_server.mcp.ui.server import EndTurnInput
from mcp_infra.constants import POLICY_PROPOSER_MOUNT_PREFIX, UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function

# Skip if Playwright is not installed
playwright = pytest.importorskip("playwright.sync_api")

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = playwright.Page


class ProposalUiMock(DecoratorMock):
    """Mock with policy proposer and UI helpers."""

    def create_proposal(self, content: str):
        """Create policy proposal tool call."""
        return self.tool_call(
            build_mcp_function(POLICY_PROPOSER_MOUNT_PREFIX, "create_proposal"), CreateProposalArgs(content=content)
        )

    def end_turn(self):
        """Create end_turn tool call."""
        return self.tool_call(build_mcp_function(UI_MOUNT_PREFIX, "end_turn"), EndTurnInput())


async def test_policy_proposal_reject_updates_ui(e2e_page, run_server, policy_allow_all: str):
    """E2E: a policy proposal appears; rejecting it removes it from Open Proposals without reload."""

    # Mock the agent to create a proposal via tool call, then end turn
    @ProposalUiMock.mock()
    def mock(m: ProposalUiMock):
        yield
        yield m.create_proposal(policy_allow_all)
        yield m.end_turn()

    s = run_server(lambda model: mock)

    e2e_page.goto(s.base_url)
    e2e_page.create_agent_via_ui()

    # Trigger agent to create a proposal by sending a prompt
    e2e_page.send_prompt("create policy proposal")

    # Wait for the run to finish (agent created proposal and ended turn)
    e2e_page.wait_for_text("Status: finished")

    # Reload page to pick up the proposal
    e2e_page.reload_and_reconnect()

    # Open proposal should appear in the Approvals tab
    e2e_page.wait_for_text("Open Proposals (1)")

    # Reject it
    e2e_page.click_reject()

    # The open proposals section should disappear (no open proposals remain)
    e2e_page.page.get_by_text("Open Proposals (1)").wait_for(state="detached", timeout=10000)

    s.stop()


if __name__ == "__main__":
    pytest_bazel.main()
