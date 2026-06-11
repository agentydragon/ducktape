"""Helper functions for grading edges and submitting gradings.

These helpers simplify the grading workflow for the bipartite graph model.
Each edge represents a grader's judgment about whether a critique issue matches
a GT occurrence.

Database session is obtained automatically using get_session() which respects
the grader agent's RLS-scoped credentials.

Typical workflow:
    1. Call get_pending_edges() to see what edges need grading
    2. Call insert_edge() for each (issue, occurrence) pair
    3. Call submit_grading() to finalize and mark the grading complete
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select, text

from agent_pkg.runtime.mcp import mcp_client_from_env
from props.core.db.models import AgentRun, GradingEdge, GradingPending, ReportedIssue
from props.core.db.session import get_session


def get_pending_edges(
    issue_filter: str | None = None, gt_filter: str | None = None, critique_run_id: UUID | None = None
) -> list[GradingPending]:
    """Get pending (missing) grading edges from the grading_pending view.

    Args:
        issue_filter: Filter to specific critique issue ID
        gt_filter: Filter to specific GT occurrence (format: tp/<id>/<occ> or fp/<id>/<occ>)
        critique_run_id: Filter to specific critic run (for snapshot mode)

    Returns:
        List of GradingPending ORM objects.
    """
    with get_session() as session:
        query = select(GradingPending)

        if critique_run_id:
            query = query.where(GradingPending.critique_run_id == critique_run_id)

        if issue_filter:
            query = query.where(GradingPending.critique_issue_id == issue_filter)

        if gt_filter:
            parts = gt_filter.split("/")
            if len(parts) == 3:
                gt_type, gt_id, occ_id = parts
                if gt_type == "tp":
                    query = query.where(GradingPending.tp_id == gt_id, GradingPending.tp_occurrence_id == occ_id)
                else:
                    query = query.where(GradingPending.fp_id == gt_id, GradingPending.fp_occurrence_id == occ_id)

        return list(session.scalars(query))


def insert_edge(
    critique_issue_id: str,
    credit: float,
    rationale: str,
    tp_id: str | None = None,
    tp_occurrence_id: str | None = None,
    fp_id: str | None = None,
    fp_occurrence_id: str | None = None,
    critique_run_id: UUID | None = None,
) -> None:
    """Insert a grading edge for a (critique_issue, gt_occurrence) pair.

    Exactly one of (tp_id, tp_occurrence_id) or (fp_id, fp_occurrence_id) must be set.

    Args:
        critique_issue_id: ID of the critique issue
        credit: Credit value (0.0-1.0)
        rationale: Explanation for the edge
        tp_id: TP ID (if matching to TP)
        tp_occurrence_id: TP occurrence ID (if matching to TP)
        fp_id: FP ID (if matching to FP)
        fp_occurrence_id: FP occurrence ID (if matching to FP)
        critique_run_id: Critic run ID (required in snapshot mode, derived in single-critique mode)
    """
    with get_session() as session:
        # Get current grader run ID from PostgreSQL function
        grader_run_id = session.scalar(text("SELECT current_agent_run_id()"))
        if grader_run_id is None:
            raise RuntimeError("current_agent_run_id() returned NULL - not connected as agent user")

        # Get critique run info if not provided
        if critique_run_id is None:
            issue = session.query(ReportedIssue).filter(ReportedIssue.issue_id == critique_issue_id).first()
            if issue is None:
                raise ValueError(f"Critique issue not found: {critique_issue_id}")
            critique_run_id = issue.agent_run_id

        # Get snapshot slug from the critic run's config
        critic_run = session.get(AgentRun, critique_run_id)
        if critic_run is None:
            raise ValueError(f"Critic run not found: {critique_run_id}")
        snapshot_slug = critic_run.critic_config().example.snapshot_slug

        edge = GradingEdge(
            critique_run_id=critique_run_id,
            critique_issue_id=critique_issue_id,
            snapshot_slug=snapshot_slug,
            tp_id=tp_id,
            tp_occurrence_id=tp_occurrence_id,
            fp_id=fp_id,
            fp_occurrence_id=fp_occurrence_id,
            credit=credit,
            rationale=rationale,
            grader_run_id=grader_run_id,
        )
        session.add(edge)


def delete_edges_for_issue(critique_issue_id: str, critique_run_id: UUID | None = None) -> int:
    """Delete all grading edges for a critique issue.

    Args:
        critique_issue_id: ID of the critique issue
        critique_run_id: Critic run ID (required in snapshot mode to disambiguate)

    Returns:
        Number of edges deleted
    """
    with get_session() as session:
        grader_run_id = session.scalar(text("SELECT current_agent_run_id()"))

        stmt = delete(GradingEdge).where(
            GradingEdge.critique_issue_id == critique_issue_id, GradingEdge.grader_run_id == grader_run_id
        )
        if critique_run_id:
            stmt = stmt.where(GradingEdge.critique_run_id == critique_run_id)

        result = session.execute(stmt)
        return result.rowcount  # type: ignore[attr-defined,no-any-return]


async def submit_grading(summary: str) -> None:
    """Call the MCP submit tool to finalize the grading.

    This marks the grading as complete. Fails if any edges are still pending.

    Args:
        summary: Brief summary of the grading results
    """
    async with mcp_client_from_env() as (client, _init_result):
        result = await client.call_tool("submit", {"summary": summary})
        if result.is_error:
            raise RuntimeError(f"Submit failed: {result.content}")
