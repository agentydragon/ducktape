"""MCP server for critic submit workflow.

Provides the submit tool that agents call when done reviewing.
Validates the critique and marks the agent run as complete.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.tools import FunctionTool

from mcp_infra.enhanced.server import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.core.db.models import AgentRun, AgentRunStatus, ReportedIssue, ReportedIssueOccurrence
from props.core.db.session import get_session
from props.core.db.snapshots import DBLocationAnchor
from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec

logger = logging.getLogger(__name__)


class CriticSubmitInput(OpenAIStrictModeBaseModel):
    """Input for submit tool."""

    issues_count: int
    summary: str


class ReportFailureInput(OpenAIStrictModeBaseModel):
    """Input for report_failure tool."""

    message: str


class CriticSubmitServer(EnhancedFastMCP):
    """MCP server for critic submit operations.

    Provides the submit tool that validates and finalizes an agent run,
    and the report_failure tool for agent-reported failures.

    The agent discovers its run ID via current_agent_run_id() SQL function
    which extracts it from the database username pattern (agent_{uuid}).
    """

    # Tool attributes
    submit_tool: FunctionTool
    report_failure_tool: FunctionTool

    def __init__(
        self, *, agent_run_id: UUID, snapshot_slug: SnapshotSlug, example: ExampleSpec, auth: AuthProvider | None = None
    ):
        """Initialize critic submit server.

        Args:
            agent_run_id: UUID of the agent run to finalize
            snapshot_slug: Snapshot slug (for resource URIs)
            example: Example specification (snapshot + scope)
            auth: Auth provider for HTTP mode (optional)
        """
        super().__init__("Critic Submit", instructions="Submit completed critic review with validation", auth=auth)
        self._agent_run_id = agent_run_id
        self._snapshot_slug = snapshot_slug
        self._example = example

        # Note: Agent discovers example (snapshot_slug + scope) from its agent_run row via database.
        # No MCP resources needed for these values.

        def submit(input: CriticSubmitInput) -> None:
            """Finalize critic review and validate reported issues.

            Call this when you're done reviewing code. This will:
            1. Validate all reported issues and occurrences
            2. Mark the critic run as completed
            3. Store your summary

            Validations performed:
            - Issues count must match actual reported issues in database
            - Every issue must have at least one occurrence
            - Each occurrence must have at least one location
            - Line ranges must be valid (start_line > 0, end_line >= start_line)
            """
            with get_session() as session:
                agent_run = session.get(AgentRun, self._agent_run_id)
                if agent_run is None:
                    raise ToolError(f"Agent run {self._agent_run_id} not found")

                if agent_run.status == AgentRunStatus.COMPLETED:
                    raise ToolError(f"Agent run {self._agent_run_id} already completed")

                issues = session.query(ReportedIssue).filter_by(agent_run_id=self._agent_run_id).all()

                actual_issues_count = len(issues)
                if input.issues_count != actual_issues_count:
                    raise ToolError(
                        f"Issues count mismatch: expected {input.issues_count} but found {actual_issues_count} in database"
                    )

                total_occurrences = 0
                for issue in issues:
                    occurrences = (
                        session.query(ReportedIssueOccurrence)
                        .filter_by(agent_run_id=self._agent_run_id, reported_issue_id=issue.issue_id)
                        .all()
                    )

                    if len(occurrences) == 0:
                        raise ToolError(
                            f"Issue '{issue.issue_id}' has no occurrences. "
                            f"Every issue must have at least one occurrence showing where it occurs in the code."
                        )

                    total_occurrences += len(occurrences)

                    for occ in occurrences:
                        self._validate_occurrence(occ)

                agent_run.status = AgentRunStatus.COMPLETED
                agent_run.completion_summary = input.summary
                session.commit()

                logger.info(
                    "Agent run %s completed: %d issues, %d occurrences",
                    self._agent_run_id,
                    len(issues),
                    total_occurrences,
                )

        self.submit_tool = self.flat_model()(submit)

        def report_failure(input: ReportFailureInput) -> None:
            """Report that critique could not be completed.

            Call this when you encounter blocking issues that prevent review completion
            (e.g., no files matched scope, access issues, missing dependencies).

            This marks the run as failed and stores the error message.
            """
            with get_session() as session:
                agent_run = session.get(AgentRun, self._agent_run_id)
                if agent_run is None:
                    raise ToolError(f"Agent run {self._agent_run_id} not found")

                if agent_run.status == AgentRunStatus.COMPLETED:
                    raise ToolError(f"Agent run {self._agent_run_id} already completed")

                if agent_run.status == AgentRunStatus.REPORTED_FAILURE:
                    raise ToolError(f"Agent run {self._agent_run_id} already reported failure")

                agent_run.status = AgentRunStatus.REPORTED_FAILURE
                agent_run.completion_summary = input.message
                session.commit()

                logger.info("Agent run %s reported failure: %s", self._agent_run_id, input.message)

        self.report_failure_tool = self.flat_model()(report_failure)

    def _validate_occurrence(self, occ: ReportedIssueOccurrence) -> None:
        """Validate a single occurrence.

        Raises ToolError if validation fails.

        Note: File path existence validation is not performed since the snapshot
        lives inside the container (fetched by the agent at init time). We only
        validate that locations are structurally valid.
        """
        # Check that locations is not empty
        if not occ.locations or len(occ.locations) == 0:
            raise ToolError(f"Occurrence {occ.id} must have at least one location")

        # Validate each location
        for i, loc in enumerate(occ.locations):
            # Type check (should be DBLocationAnchor from Pydantic)
            if not isinstance(loc, DBLocationAnchor):
                raise ToolError(f"Location {i} must be a DBLocationAnchor, got {type(loc)}")

            # Validate line ranges if provided
            if loc.start_line is not None:
                if loc.start_line <= 0:
                    raise ToolError(f"Location {i}: start_line must be > 0, got {loc.start_line}")

                if loc.end_line is not None and loc.end_line < loc.start_line:
                    raise ToolError(f"Location {i}: end_line ({loc.end_line}) must be >= start_line ({loc.start_line})")
