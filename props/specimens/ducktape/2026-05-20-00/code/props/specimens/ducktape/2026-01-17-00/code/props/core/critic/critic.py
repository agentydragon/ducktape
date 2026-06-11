"""Critic agent environment.

Provides CriticAgentEnvironment for running critic agents. The actual execution
logic is in AgentRegistry.run_critic().

TODO: Enable compaction for critic runs to reduce transcript size.
TODO: Do not install adgn package into critic container - snapshots contain past versions of adgn
      and installing current adgn would create conflicts/pollution in the review environment.
"""

from uuid import UUID

import aiodocker
from fastmcp.server.auth import AuthProvider

from mcp_infra.enhanced.server import EnhancedFastMCP
from props.core.agent_setup import AgentEnvironment
from props.core.agent_workspace import WorkspaceManager
from props.core.critic.submit_server import CriticSubmitServer
from props.core.db.config import DatabaseConfig
from props.core.display import short_uuid
from props.core.models.examples import ExampleSpec


class CriticAgentEnvironment(AgentEnvironment):
    """Agent environment for HTTP-mode critic with critic_submit tool.

    Provides complete environment for critic agents:
    - Temporary database user with RLS scoping (agent_{run_id})
    - HTTP MCP server with critic_submit tool
    - Docker container with docker_exec

    Snapshots are fetched by the agent at init time via fetch_snapshot()
    from props.core.agent_helpers. No bind mounts for snapshots.

    Agent workflow:
    1. Init script fetches snapshot to /snapshots/<slug>/
    2. Agent reviews code at /snapshots/<slug>/
    3. Writes reported issues directly to PostgreSQL
    4. Calls critic_submit tool via MCP-over-HTTP when done
    5. Submit validates decisions and marks run complete

    Usage:
        async with CriticAgentEnvironment(
            example=WholeSnapshotExample(snapshot_slug="ducktape/2025-11-26-00"),
            docker_client=docker_client,
            agent_run_id=run_id,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image="localhost:5050/critic@sha256:abc...",  # Full OCI reference
        ) as compositor:
            # Run critic agent
            ...
    """

    def __init__(
        self,
        example: ExampleSpec,
        docker_client: aiodocker.Docker,
        agent_run_id: UUID,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        *,
        image: str,
        container_name: str | None = None,
    ):
        # Store params needed by _make_mcp_server (before super().__init__ since it accesses them)
        self._example = example

        name = container_name or f"critic-{short_uuid(agent_run_id)}"

        super().__init__(
            agent_run_id=agent_run_id,
            docker_client=docker_client,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image=image,
            container_name=name,
            labels={"adgn.project": "props", "adgn.role": "critic", "adgn.agent_run_id": str(agent_run_id)},
            auto_remove=True,
        )

    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        return CriticSubmitServer(
            agent_run_id=self._agent_run_id, snapshot_slug=self._example.snapshot_slug, example=self._example, auth=auth
        )
