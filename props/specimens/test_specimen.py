"""Generic single-specimen validation test.

This test is instantiated once per specimen by the specimen_targets macro.
Test parameters (code tar, data YAML) are passed via environment variables.
The slug is read from the data YAML.
"""

from __future__ import annotations

import os
import tarfile

import pytest_bazel

from props.db.database import Database
from props.db.models import Snapshot
from props.db.sync.sync import SpecimenBundle, sync_specimen
from util.bazel.runfiles import get_required_path

_CODE_TAR = get_required_path(f"_main/{os.environ['SPECIMEN_CODE_TAR']}")
_DATA_YAML = get_required_path(f"_main/{os.environ['SPECIMEN_DATA_YAML']}")


def test_specimen_code_excludes_specimens_dataset() -> None:
    """props/specimens (this dataset) must never be nested inside a specimen's code."""
    with tarfile.open(_CODE_TAR) as tar:
        nested = [name for name in tar.getnames() if name == "props/specimens" or name.startswith("props/specimens/")]
    assert not nested, f"Specimen code contains nested props/specimens paths: {nested}"


def test_specimen(db: Database) -> None:
    """Verify specimen syncs successfully and has expected content."""
    # Create specimen bundle (reads slug from data YAML)
    bundle = SpecimenBundle.from_paths(_CODE_TAR, _DATA_YAML)

    # Sync from bundle artifacts
    with db.session() as session:
        sync_specimen(session, bundle)
        session.commit()

    # Verify sync succeeded
    with db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=bundle.slug).one_or_none()
        assert snapshot is not None, f"Specimen {bundle.slug} was not synced"
        assert snapshot.content is not None, f"Specimen {bundle.slug} has no content tar"

        tp_count = len(snapshot.true_positives)
        assert tp_count > 0, f"Specimen {bundle.slug} has no true positives"


if __name__ == "__main__":
    pytest_bazel.main()
