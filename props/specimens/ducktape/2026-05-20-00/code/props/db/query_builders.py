"""SQLAlchemy query builders for agent-accessible database queries."""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.splits import Split
from props.db.models import TpOccurrenceCredit


class RecallByExampleRow(BaseModel):
    """Single row from recall-by-example query."""

    example: ExampleSpec
    critic_image_digest: str
    recall: float
    snapshot_slug: SnapshotSlug  # For backwards compatibility with existing code


def query_recall_by_example(
    session: Session, split: Split | None = None, critic_image_digest: str | None = None
) -> list[RecallByExampleRow]:
    """Query occurrence-weighted recall grouped by (example, critic_image_digest).

    Computes AVG(found_credit) from tp_occurrence_credits view, grouped by
    (snapshot_slug, example_kind, files_hash, critic_image_digest).

    This is the canonical way to compute recall for cross-run aggregation.
    Single-run recall can be computed inline from occurrence_results.

    Args:
        session: SQLAlchemy session
        split: Optional split filter (TRAIN, VALID, TEST)
        critic_image_digest: Optional image digest filter (get recall for specific definition)

    Returns:
        List of RecallByExampleRow (example, critic_image_digest, recall)

    Example:
        # Get recall for all train examples with a specific definition
        results = query_recall_by_example(
            session,
            split=Split.TRAIN,
            critic_image_digest="sha256:abc123..."
        )
        for row in results:
            print(f"{row.example}: {row.recall * 100:.1f}%")
    """
    # Query TpOccurrenceCredit VIEW (uses example_kind + files_hash composite key)
    query = session.query(
        TpOccurrenceCredit.snapshot_slug,
        TpOccurrenceCredit.example_kind,
        TpOccurrenceCredit.files_hash,
        TpOccurrenceCredit.critic_image_digest,
        func.avg(TpOccurrenceCredit.found_credit).label("avg_credit_per_occurrence"),
    )

    if split is not None:
        query = query.filter(TpOccurrenceCredit.split == split)
    if critic_image_digest is not None:
        query = query.filter(TpOccurrenceCredit.critic_image_digest == critic_image_digest)

    query = query.group_by(
        TpOccurrenceCredit.snapshot_slug,
        TpOccurrenceCredit.example_kind,
        TpOccurrenceCredit.files_hash,
        TpOccurrenceCredit.critic_image_digest,
    )

    results = query.all()
    rows: list[RecallByExampleRow] = []
    for r in results:
        # Build ExampleSpec from query result
        if r.example_kind == ExampleKind.WHOLE_SNAPSHOT:
            example_spec: ExampleSpec = WholeSnapshotExample(snapshot_slug=r.snapshot_slug)
        elif r.example_kind == ExampleKind.FILE_SET:
            if r.files_hash is None:
                raise ValueError(f"example_kind=file_set but files_hash is NULL for {r.snapshot_slug}")
            example_spec = SingleFileSetExample(snapshot_slug=r.snapshot_slug, files_hash=r.files_hash)
        else:
            raise ValueError(f"Unknown example_kind: {r.example_kind}")

        rows.append(
            RecallByExampleRow(
                example=example_spec,
                critic_image_digest=r.critic_image_digest,
                recall=r.avg_credit_per_occurrence,
                snapshot_slug=r.snapshot_slug,
            )
        )
    return rows
