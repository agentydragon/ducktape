"""Grader daemon main entry point for in-container execution.

This is the CMD entrypoint for the daemon grader container. It:
1. Fetches the snapshot to /workspace
2. Sets up pg_notify listener for grading_pending changes
3. Runs a single persistent agent loop (sleep tool awaits notifications in-process)
4. Only exits on fatal error
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.logging_handler import LoggingHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.model import SystemMessage
from props.agents.grader.drift_handler import check_grading_pending
from props.agents.grader.notification_handler import GraderNotificationsHandler
from props.agents.grader.notifications import GRADING_PENDING_CHANNEL, GradingPendingNotification
from props.agents.grader.tools import (
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
    SleepArgs,
    TPRef,
)
from props.agents.runtime import (
    WORKSPACE,
    create_bound_model_from_env,
    get_current_agent_run,
    get_current_agent_run_id,
    render_system_prompt,
    setup_logging,
)
from props.core.ids import SnapshotSlug
from props.db.database import Database
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
from props.db.snapshot_io import fetch_snapshot_to_path

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to grade issues. Do not output text directly. "
    "Use list_pending to see pending edges, then insert_edges or fill_remaining to grade them. "
    "Call sleep when you believe all grading is complete."
)

# Default workspace path
_WORKSPACE = Path("/workspace")


class DaemonState:
    """Tracks daemon state for pg_notify wake/sleep coordination.

    Shared between the sleep tool (which awaits wake_event) and the
    pg_notify listener callback (which sets it).
    """

    def __init__(self, snapshot_slug: SnapshotSlug):
        self.snapshot_slug = snapshot_slug
        self.wake_event = asyncio.Event()
        self.failed = False
        self.notification_queue: list[GradingPendingNotification] = []

    def notification_callback(
        self, connection: asyncpg.Connection[Any] | PoolConnectionProxy[Any], pid: int, channel: str, payload: object
    ) -> None:
        """Handle incoming pg_notify notifications."""
        if not isinstance(payload, str):
            raise TypeError(f"Expected string payload, got {type(payload)}")

        notification = GradingPendingNotification.model_validate_json(payload)

        if notification.snapshot_slug != self.snapshot_slug:
            return  # Not for us

        logger.debug(f"Notification for {self.snapshot_slug}: {notification.operation} {notification.item.table}")
        self.notification_queue.append(notification)
        self.wake_event.set()


def _make_gt_ref(pending: GradingPending) -> TPRef | FPRef:
    """Create a GTRef from a GradingPending row."""
    if pending.tp_id:
        return TPRef(tp_id=pending.tp_id, occurrence_id=pending.tp_occurrence_id)
    return FPRef(fp_id=pending.fp_id, occurrence_id=pending.fp_occurrence_id)


def _create_grader_tool_provider(
    grader_run_id: UUID, snapshot_slug: SnapshotSlug, state: DaemonState, db: Database
) -> DirectToolProvider:
    """Create a tool provider with grader tools bound to the given run."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command. Use for file operations, database queries, etc."""
        return await run_direct_exec(args, default_cwd=_WORKSPACE)

    @provider.tool
    def list_pending(args: ListPendingArgs) -> list[PendingEdge]:
        """List pending grading edges from grading_pending view.

        Returns edges that still need grading decisions.
        """
        with db.session() as session:
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
        with db.session() as session:
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
        with db.session() as session:
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
        with db.session() as session:
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
        with db.session() as session:
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
        with db.session() as session:
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
        with db.session() as session:
            count = (
                session.query(GradingEdge)
                .filter_by(critique_run_id=args.run, critique_issue_id=args.issue_id, grader_run_id=grader_run_id)
                .delete()
            )

        return f"Deleted {count} edges for {args.run}/{args.issue_id}"

    @provider.tool
    def report_failure(args: ReportFailureArgs) -> None:
        """Report that grading could not be completed. Terminates the daemon."""
        state.failed = True
        logger.info("Reported failure: %s", args.message)

    @provider.tool
    async def sleep(args: SleepArgs) -> str:
        """Sleep until new grading work arrives.

        Call when all pending edges have been graded. Verifies grading_pending
        is empty, then waits for pg_notify. Returns when new work is available.
        """
        if check_grading_pending(snapshot_slug, db):
            raise ValueError("There is still pending grading work. Continue grading before sleeping.")
        logger.info("Sleep requested: %s", args.summary)
        while True:
            state.wake_event.clear()
            await state.wake_event.wait()
            if check_grading_pending(snapshot_slug, db):
                break
            logger.debug("Spurious wake — no pending work, going back to sleep")
        return "Woke up — new grading work arrived."

    return provider


async def _run_agent_loop(system_prompt: str, snapshot_slug: SnapshotSlug, state: DaemonState, db: Database) -> None:
    """Run the grader agent loop.

    Runs a single persistent agent loop. The sleep tool awaits pg_notify
    in-process, so the agent retains context across sleep/wake cycles.
    Only returns when report_failure is called (sets state.failed).
    """
    # TODO: Handle context growth across sleep/wake cycles. Options:
    # - Transcript compaction (summarize old tool results, keep recent)
    # - Context clear + agent restart when approaching limit
    with db.session() as session:
        grader_run_id = get_current_agent_run_id(session)

    tool_provider = _create_grader_tool_provider(grader_run_id, snapshot_slug, state, db)
    bound_model = create_bound_model_from_env(db)

    handlers: list[BaseHandler] = [
        LoggingHandler(logger),
        AbortIf(lambda: state.failed),
        GraderNotificationsHandler(state.notification_queue),
        RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
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
    logger.error("Grading failed via report_failure")


async def _run_daemon(snapshot_slug: SnapshotSlug, system_prompt: str, db: Database) -> None:
    """Set up pg_notify listener and run the agent loop.

    Only returns when report_failure is called (always a failure).
    Normal operation is an infinite sleep/wake loop.
    """
    state = DaemonState(snapshot_slug)

    listener_conn = await db.config.asyncpg_connect()
    await listener_conn.add_listener(GRADING_PENDING_CHANNEL, state.notification_callback)
    logger.info(f"Listening on channel '{GRADING_PENDING_CHANNEL}' for {snapshot_slug}")

    try:
        await _run_agent_loop(system_prompt, snapshot_slug, state, db)
    finally:
        await listener_conn.remove_listener(GRADING_PENDING_CHANNEL, state.notification_callback)
        await listener_conn.close()
        logger.info("Listener stopped")


async def main() -> int:
    """Main entry point for daemon grader agent."""
    setup_logging()

    logger.info("Grader daemon starting")
    db = Database.from_env()

    with db.session() as session:
        agent_run = get_current_agent_run(session)
        config = agent_run.grader_config()
        snapshot_slug = SnapshotSlug(config.snapshot_slug)
        logger.info("Agent run: %s, snapshot: %s, model: %s", agent_run.agent_run_id, snapshot_slug, agent_run.model)

    logger.info("Fetching snapshot to %s", WORKSPACE)
    fetch_snapshot_to_path(snapshot_slug, WORKSPACE, db)

    logger.info("Rendering system prompt")
    system_prompt = render_system_prompt(
        "props/agents/grader/prompt.md.mako", db, helpers={"snapshot_slug": snapshot_slug}
    )

    logger.info("Starting daemon loop")
    await _run_daemon(snapshot_slug, system_prompt, db)
    # _run_daemon only returns on report_failure
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
