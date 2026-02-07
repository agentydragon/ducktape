"""Grading status queries — direct DB access inside containers."""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from sqlalchemy import func

from props.agents.runtime import get_current_agent_run_id
from props.core.eval_api_models import GradingStatusResponse
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, GradingEdge, GradingPending, RecallByRun, Snapshot

logger = logging.getLogger(__name__)


def get_grading_status_from_db(critic_run_id: UUID, db: Database) -> GradingStatusResponse:
    """Check grading status by querying the database directly."""
    with db.session() as session:
        pending_count = (
            session.query(func.count())
            .select_from(GradingPending)
            .filter(GradingPending.critique_run_id == critic_run_id)
            .scalar()
            or 0
        )

        if pending_count > 0:
            return GradingStatusResponse(is_complete=False, pending_count=pending_count)

        recall_row = session.get(RecallByRun, critic_run_id)
        grader_run_ids = [
            row[0]
            for row in session.query(GradingEdge.grader_run_id)
            .filter(GradingEdge.critique_run_id == critic_run_id)
            .distinct()
            .all()
        ]

        if recall_row:
            return GradingStatusResponse(
                is_complete=True,
                pending_count=0,
                grader_run_ids=grader_run_ids,
                total_credit=recall_row.total_credit,
                max_credit=recall_row.recall_denominator,
                split=recall_row.split,
                example_kind=recall_row.example_kind,
            )

        critic_run = session.get(AgentRun, critic_run_id)
        if not critic_run:
            raise ValueError(f"Critic run {critic_run_id} not found")

        critic_config = critic_run.critic_config()
        snapshot = session.query(Snapshot).filter_by(slug=critic_config.example.snapshot_slug).one()

        return GradingStatusResponse(
            is_complete=True,
            pending_count=0,
            grader_run_ids=grader_run_ids,
            total_credit=0.0,
            max_credit=0,
            split=snapshot.split,
            example_kind=critic_config.example.kind,
        )


async def wait_until_graded(
    critic_run_id: UUID, db: Database, *, timeout_seconds: int = 300, poll_interval_seconds: int = 5
) -> GradingStatusResponse:
    """Wait for a critic run to be fully graded by polling the database.

    Raises:
        ValueError: If critic run doesn't exist, isn't finished, or wasn't started by this agent
        TimeoutError: If grading doesn't complete within timeout
    """
    with db.session() as session:
        critic_run = session.get(AgentRun, critic_run_id)
        if critic_run is None:
            raise ValueError(f"Critic run {critic_run_id} not found")

        if critic_run.status == AgentRunStatus.IN_PROGRESS:
            raise ValueError(
                f"Critic run {critic_run_id} is still in progress (status: {critic_run.status}). "
                f"wait_until_graded only works on finished runs."
            )

        current_agent_id = get_current_agent_run_id(session)
        if critic_run.parent_agent_run_id != current_agent_id:
            raise ValueError(
                f"Critic run {critic_run_id} was not started by this agent. "
                f"Expected parent {current_agent_id}, got {critic_run.parent_agent_run_id}."
            )

    start_time = time.monotonic()
    deadline = start_time + timeout_seconds
    last_pending_count: int | None = None

    while time.monotonic() < deadline:
        status = get_grading_status_from_db(critic_run_id, db)

        if status.is_complete:
            return status

        if last_pending_count != status.pending_count:
            logger.debug(f"Waiting for grading: {status.pending_count} edges pending")
            last_pending_count = status.pending_count

        await asyncio.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"Timeout waiting for critic run {critic_run_id} to be graded. "
        f"Waited {timeout_seconds} seconds, {last_pending_count} edges still pending."
    )
