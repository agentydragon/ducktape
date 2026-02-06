"""Critic agent main entry point for in-container execution.

This is the CMD entrypoint for the critic container. It:
1. Fetches the snapshot to /workspace
2. Renders the system prompt
3. Runs the agent loop until submit succeeds or failure
4. Exits with appropriate code
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.model import BoundOpenAIModel, SystemMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.core.agent_helpers import fetch_snapshot, get_current_agent_run, get_current_agent_run_id
from props.core.loop_utils import render_template_string
from props.core.models.examples import WholeSnapshotExample
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, FileSet, ReportedIssue, ReportedIssueOccurrence
from props.db.snapshots import DBLocationAnchor

# --- Tool argument models ---


class InsertIssueArgs(OpenAIStrictModeBaseModel):
    issue_id: str = Field(..., description="Unique identifier for this issue (kebab-case slug)")
    rationale: str = Field(..., description="Explanation of why this is an issue")


class InsertOccurrenceArgs(OpenAIStrictModeBaseModel):
    issue_id: str = Field(..., description="ID of the issue this occurrence belongs to")
    file: str = Field(..., description="File path relative to workspace root")
    start_line: int | None = Field(None, description="Starting line number")
    end_line: int | None = Field(None, description="Ending line number")


class LocationSpec(OpenAIStrictModeBaseModel):
    file: str
    start_line: int | None = None
    end_line: int | None = None


class InsertOccurrenceMultiArgs(OpenAIStrictModeBaseModel):
    issue_id: str = Field(..., description="ID of the issue this occurrence belongs to")
    locations: list[LocationSpec] = Field(..., description="List of locations for this occurrence")


class DeleteIssueArgs(OpenAIStrictModeBaseModel):
    issue_id: str = Field(..., description="ID of the issue to delete")


class SubmitArgs(OpenAIStrictModeBaseModel):
    issues_count: int = Field(..., description="Total number of issues reported")
    summary: str = Field(..., description="Brief summary of the code review findings")


class ReportFailureArgs(OpenAIStrictModeBaseModel):
    message: str = Field(..., description="Description of why the critique could not be completed")


# --- Tool response models ---


class LocationInfo(BaseModel):
    file: str
    start_line: int | None
    end_line: int | None


class OccurrenceInfo(BaseModel):
    locations: list[LocationInfo]


class IssueInfo(BaseModel):
    issue_id: str
    rationale: str
    occurrences: list[OccurrenceInfo]


class ListIssuesResponse(BaseModel):
    issues: list[IssueInfo]


logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to analyze code and report issues. Do not output text directly. "
    "Use exec to examine files, insert_issue/insert_occurrence to report findings, then call submit when done."
)


@dataclass
class ExitState:
    """Tracks whether a tool has requested exit."""

    should_exit: bool = False
    exit_code: int = 0


def _create_tool_provider(exit_state: ExitState, db: Database) -> DirectToolProvider:
    """Create a tool provider with critic tools."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command in the workspace. Use for code analysis tools like cat, rg, grep, find, etc."""
        return await run_direct_exec(args, default_cwd=WORKSPACE)

    @provider.tool
    def insert_issue(args: InsertIssueArgs) -> str:
        """Insert a reported issue. Call this before adding occurrences for the issue."""
        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            issue = ReportedIssue(agent_run_id=agent_run_id, issue_id=args.issue_id, rationale=args.rationale)
            session.add(issue)
        return f"Inserted issue: {args.issue_id}"

    @provider.tool
    def insert_occurrence(args: InsertOccurrenceArgs) -> str:
        """Insert a single-location occurrence for a reported issue. The issue must exist first."""
        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            occurrence = ReportedIssueOccurrence(
                agent_run_id=agent_run_id,
                reported_issue_id=args.issue_id,
                locations=[DBLocationAnchor(file=args.file, start_line=args.start_line, end_line=args.end_line)],
            )
            session.add(occurrence)

        location = args.file
        if args.start_line is not None:
            location += f":{args.start_line}"
            if args.end_line is not None and args.end_line != args.start_line:
                location += f"-{args.end_line}"
        return f"Inserted occurrence for {args.issue_id}: {location}"

    @provider.tool
    def insert_occurrence_multi(args: InsertOccurrenceMultiArgs) -> str:
        """Insert a multi-location occurrence (e.g., duplication across files). Use for issues spanning multiple locations."""
        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            occurrence = ReportedIssueOccurrence(
                agent_run_id=agent_run_id,
                reported_issue_id=args.issue_id,
                locations=[
                    DBLocationAnchor(file=loc.file, start_line=loc.start_line, end_line=loc.end_line)
                    for loc in args.locations
                ],
            )
            session.add(occurrence)
        return f"Inserted multi-location occurrence for {args.issue_id}: {len(args.locations)} locations"

    @provider.tool
    def delete_issue(args: DeleteIssueArgs) -> str:
        """Delete a reported issue and all its occurrences. Use to remove incorrect issues."""
        with db.session() as session:
            issue = session.query(ReportedIssue).filter_by(issue_id=args.issue_id).first()
            if issue is None:
                raise ValueError(f"Issue not found: {args.issue_id}")
            session.delete(issue)
        return f"Deleted issue: {args.issue_id}"

    @provider.tool
    def list_issues() -> str:
        """List all issues reported in this critique run. Returns JSON with issue IDs, rationales, and occurrences."""
        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            issues = session.query(ReportedIssue).filter_by(agent_run_id=agent_run_id).all()

            issue_infos = []
            for issue in issues:
                occurrences = (
                    session.query(ReportedIssueOccurrence)
                    .filter_by(agent_run_id=agent_run_id, reported_issue_id=issue.issue_id)
                    .all()
                )
                occurrence_infos = [
                    OccurrenceInfo(
                        locations=[
                            LocationInfo(file=loc.file, start_line=loc.start_line, end_line=loc.end_line)
                            for loc in occ.locations
                        ]
                    )
                    for occ in occurrences
                ]
                issue_infos.append(
                    IssueInfo(issue_id=issue.issue_id, rationale=issue.rationale, occurrences=occurrence_infos)
                )

            return ListIssuesResponse(issues=issue_infos).model_dump_json()

    @provider.tool
    def submit(args: SubmitArgs) -> str:
        """Finalize and submit the critique. Validates all issues and marks the run as complete."""
        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            agent_run = session.get(AgentRun, agent_run_id)

            if agent_run is None:
                raise ValueError(f"Agent run {agent_run_id} not found")

            if agent_run.status == AgentRunStatus.COMPLETED:
                raise ValueError(f"Agent run {agent_run_id} already completed")

            issues = session.query(ReportedIssue).filter_by(agent_run_id=agent_run_id).all()

            actual_issues_count = len(issues)
            if args.issues_count != actual_issues_count:
                raise ValueError(
                    f"Issues count mismatch: expected {args.issues_count} but found {actual_issues_count} in database"
                )

            total_occurrences = 0
            for issue in issues:
                occurrences = (
                    session.query(ReportedIssueOccurrence)
                    .filter_by(agent_run_id=agent_run_id, reported_issue_id=issue.issue_id)
                    .all()
                )

                if len(occurrences) == 0:
                    raise ValueError(
                        f"Issue '{issue.issue_id}' has no occurrences. "
                        f"Every issue must have at least one occurrence showing where it occurs in the code."
                    )

                total_occurrences += len(occurrences)

                for occ in occurrences:
                    _validate_occurrence(occ)

            # Note: Agent cannot update its own status due to RLS.
            # Status is set by host scaffold (agent_registry) after container exits.

        exit_state.should_exit = True
        exit_state.exit_code = 0
        logger.info("Critique submitted: %d issues, %d occurrences", args.issues_count, total_occurrences)
        return f"Submitted critique: {args.issues_count} issues, {total_occurrences} occurrences"

    @provider.tool
    def report_failure(args: ReportFailureArgs) -> str:
        """Report that the critique could not be completed due to blocking issues (e.g., no files in scope)."""
        with db.session() as session:
            agent_run_id = get_current_agent_run_id(session)
            agent_run = session.get(AgentRun, agent_run_id)

            if agent_run is None:
                raise ValueError(f"Agent run {agent_run_id} not found")

            if agent_run.status == AgentRunStatus.COMPLETED:
                raise ValueError(f"Agent run {agent_run_id} already completed")

            if agent_run.status == AgentRunStatus.REPORTED_FAILURE:
                raise ValueError(f"Agent run {agent_run_id} already reported failure")

            # Note: Agent cannot update its own status due to RLS.
            # Status is set by host scaffold (agent_registry) after container exits.

        exit_state.should_exit = True
        exit_state.exit_code = 1
        logger.info("Reported failure: %s", args.message)
        return f"Reported failure: {args.message}"

    return provider


def _validate_occurrence(occ: ReportedIssueOccurrence) -> None:
    """Validate a single occurrence. Raises ValueError if invalid."""
    if not occ.locations or len(occ.locations) == 0:
        raise ValueError(f"Occurrence {occ.id} must have at least one location")

    for i, loc in enumerate(occ.locations):
        if loc.start_line is not None:
            if loc.start_line <= 0:
                raise ValueError(f"Location {i}: start_line must be > 0, got {loc.start_line}")

            if loc.end_line is not None and loc.end_line < loc.start_line:
                raise ValueError(f"Location {i}: end_line ({loc.end_line}) must be >= start_line ({loc.start_line})")


class _LoggingHandler(BaseHandler):
    """Handler that logs events for debugging."""

    def on_error(self, exc: Exception) -> None:
        logger.error("Agent error: %s", exc)
        raise exc


def _load_prompt_template() -> str:
    """Load prompt template content from env var path or default package resource."""
    prompt_path = os.environ.get("PROMPT_TEMPLATE_PATH")
    if prompt_path:
        logger.info("Using variant prompt from %s", prompt_path)
        return Path(prompt_path).read_text()

    # Default: load from package resources
    resource = importlib.resources.files("props") / "docs/agents/critic.md.j2"
    return resource.read_text()


async def _run_agent_loop(system_prompt: str, model: str, db: Database) -> int:
    """Run the critic agent loop.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    exit_state = ExitState()
    tool_provider = _create_tool_provider(exit_state, db)

    client = AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    bound_model = BoundOpenAIModel(client=client, model=model)

    handlers: list[BaseHandler] = [
        _LoggingHandler(),
        RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
        AbortIf(lambda: exit_state.should_exit),
    ]

    agent = await Agent.create(
        tool_provider=tool_provider,
        handlers=handlers,
        client=bound_model,
        parallel_tool_calls=False,
        tool_policy=AllowAnyToolOrTextMessage(),
    )

    agent.process_message(SystemMessage.text(system_prompt))

    await agent.run()

    if exit_state.should_exit:
        if exit_state.exit_code == 0:
            print("Critique submitted successfully")
        return exit_state.exit_code

    logger.warning("Agent finished without explicit exit")
    return 1


def main() -> int:
    """Main entry point for critic agent."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    logger.info("Critic agent starting")
    db = Database.from_env()

    with db.session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model
        logger.info("Agent run: %s, model: %s", agent_run.agent_run_id, model)

        example = agent_run.critic_config().example
        snapshot_slug = example.snapshot_slug
        if isinstance(example, WholeSnapshotExample):
            scope_files = None
        else:
            file_set = (
                session.query(FileSet)
                .filter_by(snapshot_slug=example.snapshot_slug, files_hash=example.files_hash)
                .one()
            )
            scope_files = [member.file_path for member in file_set.members]

    logger.info("Fetching snapshot to %s", WORKSPACE)
    fetch_snapshot(WORKSPACE, db)

    logger.info("Rendering system prompt")
    template_content = _load_prompt_template()
    system_prompt = render_template_string(
        template_content, db, helpers={"snapshot_slug": snapshot_slug, "scope_files": scope_files}
    )

    logger.info("Starting agent loop")
    exit_code = asyncio.run(_run_agent_loop(system_prompt, model, db))

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
