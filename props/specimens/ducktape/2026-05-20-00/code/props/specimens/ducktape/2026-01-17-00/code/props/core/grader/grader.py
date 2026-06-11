"""Grader agent environment.

Provides GraderAgentEnvironment for running grader agents. The actual execution
logic is in AgentRegistry.run_grader().
"""

from uuid import UUID

import aiodocker
from fastmcp.server.auth import AuthProvider

from mcp_infra.enhanced.server import EnhancedFastMCP
from props.core.agent_setup import AgentEnvironment
from props.core.agent_workspace import WorkspaceManager
from props.core.db.config import DatabaseConfig
from props.core.display import short_uuid
from props.core.grader.submit_server import GraderSubmitServer
from props.core.ids import SnapshotSlug


class GraderAgentEnvironment(AgentEnvironment):
    """Agent environment for SQL-based grader with grader_submit tool.

    Provides complete environment for grader agents:
    - Temporary database user with RLS scoping (grader_agent_{run_id})
    - HTTP MCP server with grader_submit tool
    - Docker container with docker_exec

    Snapshots are fetched by the agent at init time via fetch_snapshot() from
    props.agent_helpers. No bind mounts for snapshots.

    Agent workflow:
    1. Init script fetches snapshot to /snapshots/<slug>/
    2. Reads critique and ground truth from PostgreSQL via psql
    3. Writes grading decisions directly to PostgreSQL
    4. Calls grader_submit tool via MCP-over-HTTP when done
    5. Submit validates decisions and marks run complete

    Usage:
        async with GraderAgentEnvironment(
            snapshot_slug="ducktape/2025-11-26-00",
            docker_client=docker_client,
            grader_run_id=run_id,
            critic_run_id=critic_run_id,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image="localhost:5050/grader@sha256:abc...",  # Full OCI reference
        ) as compositor:
            # Run grader agent
            ...
    """

    def __init__(
        self,
        snapshot_slug: SnapshotSlug,
        docker_client: aiodocker.Docker,
        grader_run_id: UUID,
        critic_run_id: UUID,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        *,
        image: str,
    ):
        # Store params needed by _make_mcp_server
        self._grader_run_id = grader_run_id
        self._critic_run_id = critic_run_id
        # Store snapshot_slug for reference (init script fetches it from DB)
        self._snapshot_slug = snapshot_slug

        super().__init__(
            agent_run_id=grader_run_id,
            docker_client=docker_client,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image=image,
            container_name=f"grader-{short_uuid(grader_run_id)}",
            labels={"adgn.project": "props", "adgn.role": "grader", "adgn.agent_run_id": str(grader_run_id)},
            auto_remove=True,
        )

    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        return GraderSubmitServer(grader_run_id=self._grader_run_id, critic_run_id=self._critic_run_id, auth=auth)
