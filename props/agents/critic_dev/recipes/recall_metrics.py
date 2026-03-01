"""Recipe: Checking definition recall metrics."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from props.core.splits import Split
from props.db.database import Database
from props.db.models import RecallByDefinitionSplitKind


def get_definition_leaderboard(session: Session, split: Split) -> list[RecallByDefinitionSplitKind]:
    """Query the recall leaderboard for a split, ordered by recall descending.

    Returns rows from the recall_by_definition_split_kind view which aggregates
    recall across all examples in the split.
    """
    rows = (
        session.query(RecallByDefinitionSplitKind)
        .filter(RecallByDefinitionSplitKind.split == split)
        .order_by(RecallByDefinitionSplitKind.recall_denominator.desc())
        .all()
    )
    # Sort by mean recall descending (rows without stats go last)
    return sorted(rows, key=lambda r: r.recall_stats.mean if r.recall_stats else -1.0, reverse=True)


def main() -> None:
    """Print recall metrics as JSON."""
    db = Database.from_env()
    with db.session() as session:
        leaderboard = get_definition_leaderboard(session, Split.TRAIN)
        print(
            json.dumps(
                {
                    "leaderboard": [
                        {
                            "digest": row.critic_image_digest,
                            "example_kind": str(row.example_kind),
                            "recall_mean": row.recall_stats.mean if row.recall_stats else None,
                            "recall_denominator": row.recall_denominator,
                        }
                        for row in leaderboard
                    ]
                }
            )
        )


if __name__ == "__main__":
    main()
