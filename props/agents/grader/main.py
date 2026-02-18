"""Grader main entry point for in-container execution.

This is the CMD entrypoint for the grader container. It:
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
from agent_core.handler import RedirectOnTextMessageHandler
from agent_core.logging_handler import LoggingHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec
from openai_utils.errors import ContextLengthExceededError
from openai_utils.model import SystemMessage
from props.agents.grader.drift_handler import Drift, get_drift as get_drift_fn
from props.agents.grader.notification_handler import GraderNotificationsHandler
from props.agents.grader.tools import (
    AddToClusterArgs,
    ClusterDetails,
    ClusterMemberInfo,
    ClusterSummary,
    CreateClusterArgs,
    DeleteClusterArgs,
    DeleteEdgesArgs,
    FillRemainingArgs,
    FPRef,
    GTDetails,
    InsertEdgesArgs,
    IssueDetails,
    ListClustersArgs,
    OccurrenceInfo,
    RemoveFromClusterArgs,
    ReportFailureArgs,
    ShowClusterArgs,
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
    IssueCluster,
    IssueClusterMember,
    ReportedIssue,
    ReportedIssueOccurrence,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.db.notifications import GRADING_PENDING_CHANNEL, GradingPendingNotification
from props.db.snapshot_io import fetch_snapshot_to_path

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy

logger = logging.getLogger(__name__)

# Reminder sent when agent outputs text instead of using tools
TEXT_OUTPUT_REMINDER = (
    "You must use tools to grade and cluster issues. Do not output text directly. "
    "Use list_pending to see pending edges, then insert_edges or fill_remaining to grade them. "
    "After grading, use list_clustering_pending to see issues needing clustering, "
    "then create_cluster or add_to_cluster. "
    "Call sleep when all grading and clustering is complete."
)

# Default workspace path
_WORKSPACE = Path("/workspace")


class GraderAbortError(BaseException):
    """Raised by report_failure to terminate the grader with exit code 1."""


class GraderState:
    """Tracks grader state for pg_notify wake/sleep coordination.

    Shared between the sleep tool (which awaits wake_event) and the
    pg_notify listener callback (which sets it).
    """

    def __init__(self, snapshot_slug: SnapshotSlug):
        self.snapshot_slug = snapshot_slug
        self.wake_event = asyncio.Event()
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
    grader_run_id: UUID, snapshot_slug: SnapshotSlug, state: GraderState, db: Database
) -> DirectToolProvider:
    """Create a tool provider with grader tools bound to the given run."""
    provider = DirectToolProvider()

    @provider.tool
    async def exec(args: DirectExecArgs) -> BaseExecResult:
        """Execute a shell command. Use for file operations, database queries, etc."""
        return await run_direct_exec(args, default_cwd=_WORKSPACE)

    @provider.tool
    def get_drift() -> Drift:
        """Get all pending work: grading edges and clustering issues.

        Returns combined drift for this snapshot. Use to check what work remains
        before sleeping or to get an overview of pending tasks.
        """
        return get_drift_fn(snapshot_slug, db)

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

            return IssueDetails(
                issue_id=issue.issue_id,
                critique_run_id=issue.agent_run_id,
                rationale=issue.rationale,
                occurrences=[OccurrenceInfo(locations=list(occ.locations or [])) for occ in occs],
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
        """Report that grading could not be completed. Terminates the grader."""
        raise GraderAbortError(args.message)

    @provider.tool
    async def sleep(args: SleepArgs) -> str:
        """Sleep until new work arrives.

        Call when all pending grading edges AND clustering are complete.
        Verifies both grading_pending and clustering_pending are empty,
        then waits for pg_notify. Returns when new work is available.
        """
        drift = get_drift_fn(snapshot_slug, db)
        if drift.grading:
            raise ValueError(f"There are {len(drift.grading)} pending grading edges. Continue grading before sleeping.")
        if drift.clustering:
            raise ValueError(
                f"There are {len(drift.clustering)} issues needing clustering. Cluster unmatched issues before sleeping."
            )
        logger.info("Sleep requested: %s", args.summary)
        while True:
            state.wake_event.clear()
            await state.wake_event.wait()
            if get_drift_fn(snapshot_slug, db).has_pending:
                break
            logger.debug("Spurious wake — no pending work, going back to sleep")
        return "Woke up — new work arrived."

    # --- Clustering tools ---

    @provider.tool
    def list_clusters(args: ListClustersArgs) -> list[ClusterSummary]:
        """List all clusters for this snapshot."""
        with db.session() as session:
            clusters = (
                session.query(IssueCluster)
                .filter_by(snapshot_slug=snapshot_slug)
                .order_by(IssueCluster.cluster_id)
                .all()
            )
            return [
                ClusterSummary(cluster_id=c.cluster_id, rationale=c.rationale, member_count=len(c.members))
                for c in clusters
            ]

    @provider.tool
    def show_cluster(args: ShowClusterArgs) -> ClusterDetails:
        """Show full details of a cluster including all members."""
        with db.session() as session:
            cluster = (
                session.query(IssueCluster).filter_by(snapshot_slug=snapshot_slug, cluster_id=args.cluster_id).first()
            )
            if not cluster:
                raise ValueError(f"Cluster not found: {args.cluster_id}")
            return ClusterDetails(
                cluster_id=cluster.cluster_id,
                rationale=cluster.rationale,
                members=[
                    ClusterMemberInfo(
                        critique_run_id=m.critique_run_id, critique_issue_id=m.critique_issue_id, rationale=m.rationale
                    )
                    for m in cluster.members
                ],
            )

    @provider.tool
    def create_cluster(args: CreateClusterArgs) -> str:
        """Create a new cluster with initial member issues.

        Only issues with no positive grading edges can be added.
        """
        with db.session() as session:
            cluster = IssueCluster(
                snapshot_slug=snapshot_slug,
                cluster_id=args.cluster_id,
                rationale=args.rationale,
                grader_run_id=grader_run_id,
            )
            session.add(cluster)
            for member in args.members:
                session.add(
                    IssueClusterMember(
                        snapshot_slug=snapshot_slug,
                        cluster_id=args.cluster_id,
                        critique_run_id=member.run,
                        critique_issue_id=member.issue_id,
                        rationale=member.rationale,
                        grader_run_id=grader_run_id,
                    )
                )
        return f"Created cluster '{args.cluster_id}' with {len(args.members)} members"

    @provider.tool
    def add_to_cluster(args: AddToClusterArgs) -> str:
        """Add issues to an existing cluster."""
        with db.session() as session:
            cluster = (
                session.query(IssueCluster).filter_by(snapshot_slug=snapshot_slug, cluster_id=args.cluster_id).first()
            )
            if not cluster:
                raise ValueError(f"Cluster not found: {args.cluster_id}")
            for member in args.members:
                session.add(
                    IssueClusterMember(
                        snapshot_slug=snapshot_slug,
                        cluster_id=args.cluster_id,
                        critique_run_id=member.run,
                        critique_issue_id=member.issue_id,
                        rationale=member.rationale,
                        grader_run_id=grader_run_id,
                    )
                )
        return f"Added {len(args.members)} members to cluster '{args.cluster_id}'"

    @provider.tool
    def remove_from_cluster(args: RemoveFromClusterArgs) -> str:
        """Remove an issue from a cluster. Fails if it would leave the cluster empty."""
        with db.session() as session:
            count = (
                session.query(IssueClusterMember)
                .filter_by(
                    snapshot_slug=snapshot_slug,
                    cluster_id=args.cluster_id,
                    critique_run_id=args.run,
                    critique_issue_id=args.issue_id,
                )
                .delete()
            )
            if count == 0:
                raise ValueError(f"Member not found: {args.run}/{args.issue_id} in cluster '{args.cluster_id}'")
        return f"Removed {args.run}/{args.issue_id} from cluster '{args.cluster_id}'"

    @provider.tool
    def delete_cluster(args: DeleteClusterArgs) -> str:
        """Delete a cluster and all its memberships. Members become unclustered."""
        with db.session() as session:
            count = (
                session.query(IssueCluster).filter_by(snapshot_slug=snapshot_slug, cluster_id=args.cluster_id).delete()
            )
            if count == 0:
                raise ValueError(f"Cluster not found: {args.cluster_id}")
        return f"Deleted cluster '{args.cluster_id}'"

    return provider


async def _run_agent_loop(system_prompt: str, snapshot_slug: SnapshotSlug, state: GraderState, db: Database) -> None:
    """Run a single grader agent loop with fresh context.

    Raises GraderAbortError if report_failure is called. Raises
    ContextLengthExceededError when the conversation history grows too large
    for the model's context window — the caller should restart with fresh context.
    """
    with db.session() as session:
        grader_run_id = get_current_agent_run_id(session)

    tool_provider = _create_grader_tool_provider(grader_run_id, snapshot_slug, state, db)
    bound_model = create_bound_model_from_env(db)

    agent = await Agent.create(
        tool_provider=tool_provider,
        handlers=[
            LoggingHandler(logger),
            GraderNotificationsHandler(state.notification_queue),
            RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
        ],
        client=bound_model,
        parallel_tool_calls=False,
        tool_policy=AllowAnyToolOrTextMessage(),
    )

    agent.process_message(SystemMessage.text(system_prompt))
    await agent.run()


async def _run_grader_loop(snapshot_slug: SnapshotSlug, system_prompt: str, db: Database) -> None:
    """Set up pg_notify listener and run the agent loop.

    Waits for drift before starting the agent loop. Restarts the agent from
    scratch when context length is exceeded — grading is idempotent since
    already-graded edges are excluded via grading_pending. Raises GraderAbortError
    when report_failure is called.
    """
    state = GraderState(snapshot_slug)

    listener_conn = await db.config.asyncpg_connect()
    await listener_conn.add_listener(GRADING_PENDING_CHANNEL, state.notification_callback)
    logger.info(f"Listening on channel '{GRADING_PENDING_CHANNEL}' for {snapshot_slug}")

    try:
        while True:
            # Wait for pending work before (re)starting the agent loop.
            while not get_drift_fn(snapshot_slug, db).has_pending:
                logger.info("No pending work — waiting for pg_notify")
                state.wake_event.clear()
                await state.wake_event.wait()

            logger.info("Pending work detected, starting agent loop")
            try:
                await _run_agent_loop(system_prompt, snapshot_slug, state, db)
            except ContextLengthExceededError:
                logger.warning(
                    "Context length exceeded; restarting agent loop from scratch "
                    "(already-graded edges will be skipped via grading_pending)"
                )
                # Outer loop re-checks drift and restarts with fresh context
    finally:
        await listener_conn.remove_listener(GRADING_PENDING_CHANNEL, state.notification_callback)
        await listener_conn.close()
        logger.info("Listener stopped")


async def main() -> int:
    """Main entry point for grader agent."""
    setup_logging()

    logger.info("Grader starting")
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

    logger.info("Starting grader loop")
    try:
        await _run_grader_loop(snapshot_slug, system_prompt, db)
    except GraderAbortError as e:
        logger.error("Grading failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
