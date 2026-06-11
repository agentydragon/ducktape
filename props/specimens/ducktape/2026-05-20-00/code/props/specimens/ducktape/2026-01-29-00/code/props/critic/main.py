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

from jinja2 import Environment
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.model import BoundOpenAIModel, SystemMessage
from props.core.agent_helpers import (
    fetch_snapshot,
    get_current_agent_run,
    get_current_agent_run_id,
    get_scope_description,
)
from props.db.models import AgentRun, AgentRunStatus, ReportedIssue, ReportedIssueOccurrence
from props.db.session import get_session
from props.db.snapshots import DBLocationAnchor

# --- Tool argument models ---


class InsertIssueArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(..., description="Unique identifier for this issue (kebab-case slug)")
    rationale: str = Field(..., description="Explanation of why this is an issue")


class InsertOccurrenceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(..., description="ID of the issue this occurrence belongs to")
    file: str = Field(..., description="File path relative to workspace root")
    start_line: int | None = Field(None, description="Starting line number")
    end_line: int | None = Field(None, description="Ending line number")


class LocationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    start_line: int | None = None
    end_line: int | None = None


class InsertOccurrenceMultiArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(..., description="ID of the issue this occurrence belongs to")
    locations: list[LocationSpec] = Field(..., description="List of locations for this occurrence")


class DeleteIssueArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_id: str = Field(..., description="ID of the issue to delete")


class SubmitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issues_count: int = Field(..., description="Total number of issues reported")
    summary: str = Field(..., description="Brief summary of the code review findings")


class ReportFailureArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


def _create_tool_provider(exit_state: ExitState) -> DirectToolProvider:
    """Create a tool provider with critic tools."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command in the workspace. Use for code analysis tools like cat, rg, grep, find, etc."""
        return await run_direct_exec(args, default_cwd=WORKSPACE)

    @provider.tool
    def insert_issue(args: InsertIssueArgs) -> str:
        """Insert a reported issue. Call this before adding occurrences for the issue."""
        with get_session() as session:
            agent_run_id = get_current_agent_run_id(session)
            issue = ReportedIssue(agent_run_id=agent_run_id, issue_id=args.issue_id, rationale=args.rationale)
            session.add(issue)
        return f"Inserted issue: {args.issue_id}"

    @provider.tool
    def insert_occurrence(args: InsertOccurrenceArgs) -> str:
        """Insert a single-location occurrence for a reported issue. The issue must exist first."""
        with get_session() as session:
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
        with get_session() as session:
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
        with get_session() as session:
            issue = session.query(ReportedIssue).filter_by(issue_id=args.issue_id).first()
            if issue is None:
                raise ValueError(f"Issue not found: {args.issue_id}")
            session.delete(issue)
        return f"Deleted issue: {args.issue_id}"

    @provider.tool
    def list_issues() -> str:
        """List all issues reported in this critique run. Returns JSON with issue IDs, rationales, and occurrences."""
        with get_session() as session:
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
        with get_session() as session:
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
        with get_session() as session:
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


def _setup_jinja_env(helpers: dict | None = None) -> Environment:
    """Create Jinja2 environment with standard helpers."""
    env = Environment()
    env.globals["workspace_dir"] = str(WORKSPACE)

    def include_doc(pkg_path: str, *, raw: bool = False) -> str:
        """Include doc from package resources."""
        pkg, _, p = pkg_path.partition("/")
        content = (importlib.resources.files(pkg) / p).read_text()
        if raw:
            return f'<doc source="{pkg_path}">\n{content}\n</doc>'
        rendered = env.from_string(content).render()
        return f'<doc source="{pkg_path}">\n{rendered}\n</doc>'

    def include_file(file_path: str, *, raw: bool = False) -> str:
        """Include file from filesystem."""
        content = Path(file_path).read_text()
        if raw:
            return f'<doc source="{file_path}">\n{content}\n</doc>'
        rendered = env.from_string(content).render()
        return f'<doc source="{file_path}">\n{rendered}\n</doc>'

    env.globals["include_doc"] = include_doc
    env.globals["include_file"] = include_file

    if helpers:
        env.globals.update(helpers)

    return env


def _render_template(content: str, helpers: dict | None = None) -> str:
    """Render a Jinja2 template string with helpers."""
    env = _setup_jinja_env(helpers)
    template = env.from_string(content)
    return template.render()


def _load_prompt_template() -> str:
    """Load prompt template content from env var path or default package resource."""
    prompt_path = os.environ.get("PROMPT_TEMPLATE_PATH")
    if prompt_path:
        logger.info("Using variant prompt from %s", prompt_path)
        return Path(prompt_path).read_text()

    # Default: load from package resources
    resource = importlib.resources.files("props") / "docs/agents/critic.md.j2"
    return resource.read_text()


async def _run_agent_loop(system_prompt: str, model: str) -> int:
    """Run the critic agent loop.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    exit_state = ExitState()
    tool_provider = _create_tool_provider(exit_state)

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

    with get_session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model
        logger.info("Agent run: %s, model: %s", agent_run.agent_run_id, model)

    logger.info("Fetching snapshot to %s", WORKSPACE)
    fetch_snapshot(WORKSPACE)

    logger.info("Rendering system prompt")
    template_content = _load_prompt_template()
    system_prompt = _render_template(template_content, helpers={"scope_description": get_scope_description()})

    logger.info("Starting agent loop")
    exit_code = asyncio.run(_run_agent_loop(system_prompt, model))

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
