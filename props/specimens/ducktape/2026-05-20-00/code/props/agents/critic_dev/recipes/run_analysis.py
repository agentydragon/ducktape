"""Recipe: Analyzing critic runs and costs.

Demonstrates how to inspect recent runs, their costs, child runs,
and budget status using the ORM models and views.
"""

from __future__ import annotations

import json
import sys
from uuid import UUID

from sqlalchemy.orm import Session

from props.core.agent_types import AgentType
from props.core.ids import SnapshotSlug
from props.db.database import Database
from props.db.models import AgentRun, AgentRunBudgetStatus, LLMRunCost


def get_recent_critic_runs(session: Session, snapshot_slug: SnapshotSlug, limit: int = 10) -> list[AgentRun]:
    """Get recent critic runs for a snapshot, newest first.

    Filters by agent_type in the JSONB type_config and snapshot_slug in the
    nested example field.
    """
    return (
        session.query(AgentRun)
        .filter(
            AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
            AgentRun.type_config["example"]["snapshot_slug"].astext == str(snapshot_slug),
        )
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
        .all()
    )


def get_run_costs(session: Session, agent_run_id: UUID) -> list[LLMRunCost]:
    """Get cost/token breakdown for a run (one row per model used)."""
    return session.query(LLMRunCost).filter_by(agent_run_id=agent_run_id).all()


def get_critic_dev_child_runs(session: Session, critic_dev_run_id: UUID) -> list[AgentRun]:
    """Get all child runs (critics/graders) spawned by a critic-dev run."""
    return (
        session.query(AgentRun)
        .filter_by(parent_agent_run_id=critic_dev_run_id)
        .order_by(AgentRun.created_at.asc())
        .all()
    )


def get_budget_status(session: Session, agent_run_id: UUID) -> AgentRunBudgetStatus | None:
    """Get budget tracking for an agent run (own + tree spend)."""
    return session.query(AgentRunBudgetStatus).filter_by(agent_run_id=agent_run_id).one_or_none()


def main(snapshot_slug_str: str | None = None) -> None:
    """Print run analysis data as JSON."""
    db = Database.from_env()
    with db.session() as session:
        result: dict[str, object] = {}
        if snapshot_slug_str:
            slug = SnapshotSlug(snapshot_slug_str)
            runs = get_recent_critic_runs(session, slug)
            result["recent_runs"] = [
                {"agent_run_id": str(run.agent_run_id), "status": str(run.status), "image_digest": run.image_digest}
                for run in runs
            ]
        print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
