"""Drift detection for snapshot grader."""

from __future__ import annotations

import logging
from uuid import UUID

from pydantic import BaseModel

from props.core.ids import SnapshotSlug
from props.db.database import Database
from props.db.models import ClusteringPending, GradingPending

logger = logging.getLogger(__name__)


class PendingGradingEdge(BaseModel):
    """A pending grading edge (detached from ORM session)."""

    critique_run_id: UUID
    critique_issue_id: str
    tp_id: str | None
    tp_occurrence_id: str | None
    fp_id: str | None
    fp_occurrence_id: str | None
    snapshot_slug: SnapshotSlug


class PendingClusteringIssue(BaseModel):
    """An issue needing clustering (detached from ORM session)."""

    critique_run_id: UUID
    critique_issue_id: str
    snapshot_slug: SnapshotSlug


class Drift(BaseModel):
    """All pending drift for a snapshot."""

    grading: list[PendingGradingEdge]
    clustering: list[PendingClusteringIssue]

    @property
    def has_pending(self) -> bool:
        """True if there's any pending work."""
        return bool(self.grading or self.clustering)


def get_drift(snapshot_slug: str, db: Database) -> Drift:
    """Return all pending drift (grading + clustering) for the snapshot."""
    with db.session() as session:
        grading_rows = session.query(GradingPending).filter(GradingPending.snapshot_slug == snapshot_slug).all()
        clustering_rows = (
            session.query(ClusteringPending).filter(ClusteringPending.snapshot_slug == snapshot_slug).all()
        )
        return Drift(
            grading=[
                PendingGradingEdge(
                    critique_run_id=r.critique_run_id,
                    critique_issue_id=r.critique_issue_id,
                    tp_id=r.tp_id,
                    tp_occurrence_id=r.tp_occurrence_id,
                    fp_id=r.fp_id,
                    fp_occurrence_id=r.fp_occurrence_id,
                    snapshot_slug=r.snapshot_slug,
                )
                for r in grading_rows
            ],
            clustering=[
                PendingClusteringIssue(
                    critique_run_id=r.critique_run_id,
                    critique_issue_id=r.critique_issue_id,
                    snapshot_slug=r.snapshot_slug,
                )
                for r in clustering_rows
            ],
        )
