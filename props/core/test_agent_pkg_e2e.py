"""E2E test for agent building and running custom agent images.

Tests the full workflow of an agent creating its own variant:
1. Pull existing critic manifest via proxy HTTP API
2. Create custom agent.md content with random token (prevents cross-test interference)
3. Create new OCI layer with the custom content
4. Push manifest by digest via proxy HTTP API
5. Proxy automatically creates agent_definitions row
6. Run the newly created agent image
7. Verify new agent got the custom agent.md in its system message
8. Calling agent reads output of called agent via psql

NOTE: These tests are currently skipped and need rewriting for the in-container architecture.
The old tests used mock clients passed directly to run_prompt_optimizer() and run_critic().
The new in-container architecture uses AgentRegistry methods that take model strings
and run actual containers via an LLM proxy.
"""

from __future__ import annotations

import pytest
import pytest_bazel


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
@pytest.mark.slow
@pytest.mark.skip(
    reason="Needs rewrite for in-container architecture: run_prompt_optimizer is now AgentRegistry method"
)
async def test_po_builds_custom_critic(synced_test_db, async_docker_client, noop_openai_client):
    """Test PO agent builds custom critic image via MCP tool integration.

    This is ONE integrated e2e test where:
    1. PO agent creates and pushes custom critic image with random token
    2. PO agent uses run_critic MCP tool to launch the custom critic
    3. Custom critic mock verifies system message contains the token
    4. PO agent queries critic output via psql
    5. Proxy automatically creates agent_definitions row

    The custom critic mock is provided as critic_client parameter, so when
    the PO's MCP tool call launches a critic, it uses our custom mock.

    TODO: This test needs to be rewritten to use the new in-container architecture.
    The new AgentRegistry.run_prompt_optimizer() takes model strings and runs actual
    containers via an LLM proxy, rather than accepting mock clients directly.
    """
    pytest.skip("Test needs rewrite for in-container architecture")


@pytest.mark.requires_docker
@pytest.mark.requires_postgres
@pytest.mark.skip(reason="Needs rewrite for in-container architecture: AgentRegistry.run_critic API changed")
async def test_critic_cannot_push_images(test_registry, test_snapshot, all_files_scope):
    """Test that critic agents cannot push images to registry.

    Only PO/PI agents should have registry write access.
    Critic attempting to push should get 403 Forbidden.

    TODO: This test needs to be rewritten to use the new in-container architecture.
    The new AgentRegistry.run_critic() takes model strings and timeout_seconds,
    not mock clients and max_turns.
    """
    pytest.skip("Test needs rewrite for in-container architecture")


if __name__ == "__main__":
    pytest_bazel.main()
