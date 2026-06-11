"""Train/validation/test split definitions for snapshot evaluation.

Each snapshot is explicitly assigned to 'train', 'valid', or 'test' in its manifest.yaml.
The split assignment is the single source of truth for snapshot classification.

Query splits via database ORM:
    from props.core.db.session import get_session
    from props.core.db.models import Snapshot

    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).one()
        split = snapshot.split  # Split.TRAIN, Split.VALID, or Split.TEST

        # Get all snapshots in a split
        train_snapshots = session.query(Snapshot).filter_by(split=Split.TRAIN).all()
"""

from __future__ import annotations

from enum import StrEnum


class Split(StrEnum):
    TRAIN = "train"
    VALID = "valid"
    TEST = "test"
