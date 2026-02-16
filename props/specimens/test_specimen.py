"""Generic single-specimen validation test.

This test is instantiated once per specimen by the specimen_targets macro.
Test parameters (code tar, data YAML) are passed via environment variables.
The slug is read from the data YAML.
"""

from __future__ import annotations

import os

import pytest_bazel

from bazel_util.runfiles import get_required_path
from props.db.database import Database
from props.db.models import Snapshot
from props.db.sync.sync import SpecimenBundle, sync_specimen


def test_specimen(db: Database) -> None:
    """Verify specimen syncs successfully and has expected content."""
    # Resolve runfiles paths
    code_tar_rloc = os.environ["SPECIMEN_CODE_TAR"]
    data_yaml_rloc = os.environ["SPECIMEN_DATA_YAML"]
    code_tar = get_required_path(f"_main/{code_tar_rloc}")
    data_yaml = get_required_path(f"_main/{data_yaml_rloc}")

    # Create specimen bundle (reads slug from data YAML)
    bundle = SpecimenBundle.from_paths(code_tar, data_yaml)

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
