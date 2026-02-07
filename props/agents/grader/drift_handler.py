"""Drift detection for snapshot grader daemon."""

from __future__ import annotations

import logging

from sqlalchemy import func

from props.db.database import Database
from props.db.models import GradingPending

logger = logging.getLogger(__name__)


def check_grading_pending(snapshot_slug: str, db: Database) -> int:
    """Return number of pending grading edges for the snapshot."""
    with db.session() as session:
        return (
            session.query(func.count())
            .select_from(GradingPending)
            .filter(GradingPending.snapshot_slug == snapshot_slug)
            .scalar()
            or 0
        )
