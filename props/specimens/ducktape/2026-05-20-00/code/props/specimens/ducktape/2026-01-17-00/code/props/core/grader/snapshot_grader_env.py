"""Agent environment for snapshot grader daemon.

Similar to GraderAgentEnvironment but for persistent snapshot-scoped grading.
Uses SnapshotGraderTypeConfig and supports the daemon sleep/wake loop.
"""

from __future__ import annotations

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


class SnapshotGraderAgentEnvironment(AgentEnvironment):
    """Agent environment for snapshot grader daemon.

    Like GraderAgentEnvironment but:
    - Uses SnapshotGraderTypeConfig (snapshot_slug instead of graded_agent_run_id)
    - Designed for long-running daemon (container stays up between sleep/wake cycles)
    - Grades ALL critiques for the snapshot, not just one

    The daemon loop is managed by GraderDaemonScaffold externally.

    Usage:
        async with SnapshotGraderAgentEnvironment(...) as compositor:
            async with GraderDaemonScaffold(...) as scaffold:
                drift_handler = scaffold.create_drift_handler()
                agent = Agent.create(handlers=[drift_handler, ...])
                while not scaffold.is_shutdown:
                    await agent.run()
                    notifs = await scaffold.wait_for_drift_or_notification()
                    agent.process_message(UserMessage.text(format(notifs)))
    """

    def __init__(
        self,
        snapshot_slug: SnapshotSlug,
        docker_client: aiodocker.Docker,
        grader_run_id: UUID,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        *,
        image: str,
    ):
        self._snapshot_slug = snapshot_slug
        self._grader_run_id = grader_run_id

        super().__init__(
            agent_run_id=grader_run_id,
            docker_client=docker_client,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image=image,
            container_name=f"snapshot-grader-{short_uuid(grader_run_id)}",
            labels={
                "adgn.project": "props",
                "adgn.role": "snapshot_grader",
                "adgn.agent_run_id": str(grader_run_id),
                "adgn.snapshot_slug": snapshot_slug,
            },
            auto_remove=False,  # Keep container for debugging if daemon crashes
        )

    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        # Use a minimal submit server - grader uses CLI for everything else
        # critic_run_id is not applicable for snapshot grader (grades all critiques)
        # We use grader_run_id for both to satisfy the interface
        return GraderSubmitServer(
            grader_run_id=self._grader_run_id,
            critic_run_id=self._grader_run_id,  # Not used for snapshot grader
            auth=auth,
        )

    @property
    def snapshot_slug(self) -> SnapshotSlug:
        return self._snapshot_slug
