"""Recipe: Working with examples and scopes.

Demonstrates how to list training examples.
"""

from __future__ import annotations

import json

from props.core.splits import Split
from props.db.database import Database
from props.db.examples import get_examples_for_split


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
