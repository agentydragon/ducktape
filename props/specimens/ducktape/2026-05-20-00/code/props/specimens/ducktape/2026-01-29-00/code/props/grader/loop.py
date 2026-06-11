"""In-container agent loop for grader agents using agent_core.Agent.

Supports both one-off and daemon modes via GraderMode flag:
- ONE_OFF: grades single critic run, has submit tool
- DAEMON: grades all critiques for snapshot, no submit (drift handler controls sleep)

Both modes share the same tools except for submit availability.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import select

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.model import BoundOpenAIModel, SystemMessage
from props.core.agent_helpers import get_current_agent_run_id
from props.core.ids import SnapshotSlug
from props.db.models import (
    FalsePositive,
    FalsePositiveOccurrenceORM,
    GradingEdge,
    GradingPending,
    ReportedIssue,
    ReportedIssueOccurrence,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.db.session import get_session
from props.grader.tools import (
    DeleteEdgesArgs,
    FillRemainingArgs,
    FPRef,
    GTDetails,
    InsertEdgesArgs,
    IssueDetails,
    ListPendingArgs,
    LocationInfo,
    PendingEdge,
    ReportFailureArgs,
    ShowFPArgs,
    ShowIssueArgs,
    ShowTPArgs,
    SubmitArgs,
    TPRef,
)

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to grade issues. Do not output text directly. "
    "Use list_pending to see pending edges, insert_edges to grade them, then call submit when done."
)

# Default workspace path
WORKSPACE = Path("/workspace")


class GraderMode(StrEnum):
    """Grader operation mode."""

    ONE_OFF = "one_off"  # Grades single critic run, has submit
    DAEMON = "daemon"  # Grades all critiques for snapshot, no submit


@dataclass
class ExitState:
    """Tracks whether a tool has requested exit."""

    should_exit: bool = False


def _make_gt_ref(pending: GradingPending) -> TPRef | FPRef:
    """Create a GTRef from a GradingPending row."""
    if pending.tp_id:
        return TPRef(tp_id=pending.tp_id, occurrence_id=pending.tp_occurrence_id)
    return FPRef(fp_id=pending.fp_id, occurrence_id=pending.fp_occurrence_id)


def create_grader_tool_provider(
    grader_run_id: UUID, snapshot_slug: SnapshotSlug, exit_state: ExitState, mode: GraderMode
) -> DirectToolProvider:
    """Create a tool provider with grader tools bound to the given run."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command. Use for file operations, psql queries, etc."""
        return await run_direct_exec(args, default_cwd=WORKSPACE)

    @provider.tool
    def list_pending(args: ListPendingArgs) -> list[PendingEdge]:
        """List pending grading edges from grading_pending view.

        Returns edges that still need grading decisions.
        """
        with get_session() as session:
            query = select(GradingPending).where(GradingPending.snapshot_slug == snapshot_slug)

            if args.run:
                query = query.where(GradingPending.critique_run_id == args.run)
            if args.issue:
                query = query.where(GradingPending.critique_issue_id == args.issue)
            if args.gt:
                match args.gt:
                    case TPRef(tp_id=tp_id, occurrence_id=occ_id):
                        query = query.where(GradingPending.tp_id == tp_id, GradingPending.tp_occurrence_id == occ_id)
                    case FPRef(fp_id=fp_id, occurrence_id=occ_id):
                        query = query.where(GradingPending.fp_id == fp_id, GradingPending.fp_occurrence_id == occ_id)

            pending = list(session.scalars(query))
            return [
                PendingEdge(
                    critique_run_id=p.critique_run_id,
                    critique_issue_id=p.critique_issue_id,
                    snapshot_slug=str(p.snapshot_slug),
                    gt_ref=_make_gt_ref(p),
                )
                for p in pending
            ]

    @provider.tool
    def show_issue(args: ShowIssueArgs) -> IssueDetails:
        """Show details of a critique issue including its locations."""
        with get_session() as session:
            issue = session.query(ReportedIssue).filter_by(agent_run_id=args.run, issue_id=args.issue_id).first()
            if not issue:
                raise ValueError(f"Issue not found: {args.run}/{args.issue_id}")

            occs = (
                session.query(ReportedIssueOccurrence)
                .filter_by(agent_run_id=args.run, reported_issue_id=args.issue_id)
                .all()
            )

            locations = [
                LocationInfo(file=loc.file, start_line=loc.start_line, end_line=loc.end_line)
                for occ in occs
                for loc in occ.locations or []
            ]

            return IssueDetails(
                issue_id=issue.issue_id,
                critique_run_id=issue.agent_run_id,
                rationale=issue.rationale,
                locations=locations,
            )

    @provider.tool
    def show_tp(args: ShowTPArgs) -> GTDetails:
        """Show details of a true positive occurrence."""
        with get_session() as session:
            tp = session.query(TruePositive).filter_by(snapshot_slug=snapshot_slug, tp_id=args.tp_id).first()
            if not tp:
                raise ValueError(f"TP not found: {args.tp_id}")

            occ = (
                session.query(TruePositiveOccurrenceORM)
                .filter_by(snapshot_slug=snapshot_slug, tp_id=args.tp_id, occurrence_id=args.occurrence_id)
                .first()
            )
            if not occ:
                raise ValueError(f"TP occurrence not found: {args.tp_id}/{args.occurrence_id}")

            files_dict = {str(r.file_path): (r.start_line, r.end_line) for r in occ.ranges}
            gt_ref = TPRef(tp_id=args.tp_id, occurrence_id=args.occurrence_id)
            return GTDetails(gt_ref=gt_ref, rationale=tp.rationale, files=files_dict, note=occ.note)

    @provider.tool
    def show_fp(args: ShowFPArgs) -> GTDetails:
        """Show details of a false positive occurrence."""
        with get_session() as session:
            fp = session.query(FalsePositive).filter_by(snapshot_slug=snapshot_slug, fp_id=args.fp_id).first()
            if not fp:
                raise ValueError(f"FP not found: {args.fp_id}")

            occ = (
                session.query(FalsePositiveOccurrenceORM)
                .filter_by(snapshot_slug=snapshot_slug, fp_id=args.fp_id, occurrence_id=args.occurrence_id)
                .first()
            )
            if not occ:
                raise ValueError(f"FP occurrence not found: {args.fp_id}/{args.occurrence_id}")

            files_dict = {str(r.file_path): (r.start_line, r.end_line) for r in occ.ranges}
            gt_ref = FPRef(fp_id=args.fp_id, occurrence_id=args.occurrence_id)
            return GTDetails(gt_ref=gt_ref, rationale=fp.rationale, files=files_dict, note=occ.note)

    @provider.tool
    def insert_edges(args: InsertEdgesArgs) -> str:
        """Create grading edges matching an issue to GT occurrences.

        Each edge specifies a GT reference and credit (0.0-1.0).
        Use credit=0 for non-matches, >0 for matches based on quality.
        """
        with get_session() as session:
            for edge_spec in args.edges:
                tp_id: str | None = None
                tp_occ: str | None = None
                fp_id: str | None = None
                fp_occ: str | None = None
                match edge_spec.gt_ref:
                    case TPRef(tp_id=matched_tp_id, occurrence_id=matched_tp_occ):
                        tp_id, tp_occ = matched_tp_id, matched_tp_occ
                    case FPRef(fp_id=matched_fp_id, occurrence_id=matched_fp_occ):
                        fp_id, fp_occ = matched_fp_id, matched_fp_occ

                edge = GradingEdge(
                    critique_run_id=args.run,
                    critique_issue_id=args.issue_id,
                    snapshot_slug=snapshot_slug,
                    tp_id=tp_id,
                    tp_occurrence_id=tp_occ,
                    fp_id=fp_id,
                    fp_occurrence_id=fp_occ,
                    credit=edge_spec.credit,
                    rationale=args.rationale,
                    grader_run_id=grader_run_id,
                )
                session.add(edge)

        return f"Created {len(args.edges)} edges for {args.run}/{args.issue_id}"

    @provider.tool
    def fill_remaining(args: FillRemainingArgs) -> str:
        """Fill remaining pending edges for an issue with credit=0.

        Use when you've reviewed all GT occurrences and the remaining don't match.
        expected_count is a safety check - must match actual pending count.
        """
        with get_session() as session:
            query = select(GradingPending).where(
                GradingPending.snapshot_slug == snapshot_slug,
                GradingPending.critique_run_id == args.run,
                GradingPending.critique_issue_id == args.issue_id,
            )

            pending = list(session.scalars(query))

            if len(pending) != args.expected_count:
                raise ValueError(f"Expected {args.expected_count} pending edges but found {len(pending)}")

            for p in pending:
                edge = GradingEdge(
                    critique_run_id=p.critique_run_id,
                    critique_issue_id=p.critique_issue_id,
                    snapshot_slug=snapshot_slug,
                    tp_id=p.tp_id,
                    tp_occurrence_id=p.tp_occurrence_id,
                    fp_id=p.fp_id,
                    fp_occurrence_id=p.fp_occurrence_id,
                    credit=0.0,
                    rationale=args.rationale,
                    grader_run_id=grader_run_id,
                )
                session.add(edge)

        return f"Filled {len(pending)} edges with credit=0 for {args.run}/{args.issue_id}"

    @provider.tool
    def delete_edges(args: DeleteEdgesArgs) -> str:
        """Delete all grading edges for an issue. Use to redo grading."""
        with get_session() as session:
            count = (
                session.query(GradingEdge)
                .filter_by(critique_run_id=args.run, critique_issue_id=args.issue_id, grader_run_id=grader_run_id)
                .delete()
            )

        return f"Deleted {count} edges for {args.run}/{args.issue_id}"

    # Only add submit tool for one-off mode
    if mode == GraderMode.ONE_OFF:

        @provider.tool
        def submit(args: SubmitArgs) -> None:
            """Finalize grading. Validates no pending edges remain for this grader's scope."""
            exit_state.should_exit = True
            logger.info("Grading submitted: %s", args.summary)

    @provider.tool
    def report_failure(args: ReportFailureArgs) -> None:
        """Report that grading could not be completed.

        Use when there are blocking issues. Signals exit.
        """
        exit_state.should_exit = True
        logger.info("Reported failure: %s", args.message)

    return provider


class LoggingHandler(BaseHandler):
    """Handler that logs events for debugging."""

    def on_error(self, exc: Exception) -> None:
        logger.error("Agent error: %s", exc)
        raise exc


async def run_grader_loop(system_prompt: str, model: str, snapshot_slug: SnapshotSlug, mode: GraderMode) -> int:
    """Run the grader agent loop.

    Args:
        system_prompt: The system prompt for the grader agent
        model: Model name (must match agent_run.model for proxy validation)
        snapshot_slug: Snapshot being graded
        mode: ONE_OFF or DAEMON mode

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Get agent_run_id once at the start
    with get_session() as session:
        grader_run_id = get_current_agent_run_id(session)

    # Create tool provider with shared exit state
    exit_state = ExitState()
    tool_provider = create_grader_tool_provider(grader_run_id, snapshot_slug, exit_state, mode)

    # Create OpenAI client pointing to proxy
    client = AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    bound_model = BoundOpenAIModel(client=client, model=model)

    # Create handlers
    handlers: list[BaseHandler] = [
        LoggingHandler(),
        RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
        AbortIf(lambda: exit_state.should_exit),
    ]

    # Create and run agent
    agent = await Agent.create(
        tool_provider=tool_provider,
        handlers=handlers,
        client=bound_model,
        parallel_tool_calls=False,
        tool_policy=AllowAnyToolOrTextMessage(),
    )

    # Add system prompt
    agent.process_message(SystemMessage.text(system_prompt))

    await agent.run()
    if exit_state.should_exit:
        print("Grading completed")
        return 0

    logger.warning("Agent finished without explicit exit")
    return 1
