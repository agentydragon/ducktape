"""Recipe: Querying ground truth (true positives and false positives).

Demonstrates how to explore what issues exist for snapshots, using the
ORM models and their class methods.

Run directly::

    python3 -c "from props.agents.critic_dev.recipes.ground_truth import main; main('test-fixtures/train1')"
"""

from __future__ import annotations

import json
import sys

from sqlalchemy.orm import Session, joinedload

from props.core.ids import SnapshotSlug
from props.core.splits import Split
from props.db.database import Database
from props.db.models import FalsePositive, Snapshot, TruePositive


def list_snapshots_by_split(session: Session, split: Split) -> list[Snapshot]:
    """List all snapshots for a given split."""
    return session.query(Snapshot).filter_by(split=split).order_by(Snapshot.slug).all()


def get_true_positives(session: Session, snapshot_slug: SnapshotSlug) -> list[TruePositive]:
    """Get all TPs for a snapshot with their occurrences eagerly loaded."""
    return (
        session.query(TruePositive)
        .options(joinedload(TruePositive.occurrences))
        .filter_by(snapshot_slug=snapshot_slug)
        .order_by(TruePositive.tp_id)
        .all()
    )


def get_false_positives(session: Session, snapshot_slug: SnapshotSlug) -> list[FalsePositive]:
    """Get all FPs for a snapshot with their occurrences eagerly loaded."""
    return (
        session.query(FalsePositive)
        .options(joinedload(FalsePositive.occurrences))
        .filter_by(snapshot_slug=snapshot_slug)
        .order_by(FalsePositive.fp_id)
        .all()
    )


def main(snapshot_slug_str: str | None = None) -> None:
    """Print ground truth data as JSON."""
    db = Database.from_env()
    with db.session() as session:
        snapshots = list_snapshots_by_split(session, Split.TRAIN)
        result: dict[str, object] = {"train_snapshots": [{"slug": s.slug, "split": str(s.split)} for s in snapshots]}
        if snapshot_slug_str:
            slug = SnapshotSlug(snapshot_slug_str)
            tps = get_true_positives(session, slug)
            fps = get_false_positives(session, slug)
            result["true_positives"] = [{"tp_id": tp.tp_id, "num_occurrences": len(tp.occurrences)} for tp in tps]
            result["false_positives"] = [{"fp_id": fp.fp_id, "num_occurrences": len(fp.occurrences)} for fp in fps]
        print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
