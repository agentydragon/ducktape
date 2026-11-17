from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from ai_session_archive.database import FileVersionRow
from ai_session_archive.version_lineage import CHECKPOINT_INTERVAL, version_lineage


def test_lineage_depth_limits() -> None:
    session_id = uuid4()
    checkpoint = FileVersionRow(id=uuid4(), session_id=session_id, is_checkpoint=True)
    chain = [checkpoint]
    for _ in range(CHECKPOINT_INTERVAL):
        chain.append(
            FileVersionRow(id=uuid4(), session_id=session_id, is_checkpoint=False, parent_version_id=chain[-1].id)
        )
    versions = {row.id: row for row in chain}
    db = MagicMock(spec=Session)
    db.get.side_effect = lambda _model, version_id: versions.get(version_id)

    assert version_lineage(db, chain[-2], max_delta_depth=CHECKPOINT_INTERVAL - 1) == list(reversed(chain[:-1]))
    with pytest.raises(RuntimeError, match="delta chain exceeds"):
        version_lineage(db, chain[-1], max_delta_depth=CHECKPOINT_INTERVAL - 1)
    assert version_lineage(db, chain[-1], max_delta_depth=CHECKPOINT_INTERVAL) == list(reversed(chain))


@pytest.mark.parametrize("parent_exists", [False, True])
def test_lineage_rejects_invalid_parent(parent_exists: bool) -> None:
    parent = FileVersionRow(id=uuid4(), session_id=uuid4(), is_checkpoint=True)
    child = FileVersionRow(id=uuid4(), session_id=uuid4(), is_checkpoint=False, parent_version_id=parent.id)
    db = MagicMock(spec=Session)
    db.get.return_value = parent if parent_exists else None

    with pytest.raises(RuntimeError, match="invalid parent"):
        version_lineage(db, child, max_delta_depth=CHECKPOINT_INTERVAL)
