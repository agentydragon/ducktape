"""Recipe: Working with examples and scopes.

Demonstrates how to list training examples and construct ExampleSpec objects.
"""

from __future__ import annotations

import json

from props.core.ids import SnapshotSlug
from props.core.models.examples import ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.splits import Split
from props.db.database import Database
from props.db.examples import get_examples_for_split


def build_example_spec(snapshot_slug: str, files_hash: str | None = None) -> ExampleSpec:
    """Construct an ExampleSpec from slug and optional files_hash.

    If files_hash is provided, creates a SingleFileSetExample (per-file scope).
    Otherwise, creates a WholeSnapshotExample (full-specimen scope).
    """
    slug = SnapshotSlug(snapshot_slug)
    if files_hash is not None:
        return SingleFileSetExample(snapshot_slug=slug, files_hash=files_hash)
    return WholeSnapshotExample(snapshot_slug=slug)


def main() -> None:
    """Print training examples data as JSON."""
    db = Database.from_env()
    with db.session() as session:
        examples = get_examples_for_split(session, Split.TRAIN)
        print(
            json.dumps(
                {
                    "train_examples": [
                        {
                            "snapshot_slug": e.snapshot_slug,
                            "example_kind": str(e.example_kind),
                            "files_hash": e.files_hash,
                            "recall_denominator": e.recall_denominator,
                        }
                        for e in examples
                    ]
                }
            )
        )


if __name__ == "__main__":
    main()
