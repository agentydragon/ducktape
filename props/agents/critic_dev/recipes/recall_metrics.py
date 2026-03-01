"""Recipe: Checking definition recall metrics.

Demonstrates how to query the recall leaderboard and per-example recall
breakdown using the materialized views and query_recall_by_example().

Run directly::

    python3 -c "from props.agents.critic_dev.recipes.recall_metrics import main; main()"
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from props.core.splits import Split
from props.db.database import Database
from props.db.models import RecallByDefinitionSplitKind
from props.db.query_builders import RecallByExampleRow, query_recall_by_example


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


def compare_definitions(
    session: Session, digest_a: str, digest_b: str, split: Split
) -> dict[str, RecallByDefinitionSplitKind | None]:
    """Compare two definitions' recall metrics side-by-side.

    Returns a dict keyed by digest, with the corresponding view row or None
    if no data exists for that digest.
    """
    rows = (
        session.query(RecallByDefinitionSplitKind)
        .filter(
            RecallByDefinitionSplitKind.split == split,
            RecallByDefinitionSplitKind.critic_image_digest.in_([digest_a, digest_b]),
        )
        .all()
    )
    by_digest = {r.critic_image_digest: r for r in rows}
    return {digest_a: by_digest.get(digest_a), digest_b: by_digest.get(digest_b)}


def get_per_example_recall(session: Session, critic_image_digest: str, split: Split) -> list[RecallByExampleRow]:
    """Get per-example recall breakdown for a specific definition."""
    return query_recall_by_example(session, split=split, critic_image_digest=critic_image_digest)


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
