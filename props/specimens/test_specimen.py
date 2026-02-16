"""Generic single-specimen validation test.

This test is instantiated once per specimen by the specimen_targets macro.
Test parameters (slug, code tar, data YAML) are passed via environment variables.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
import pytest_bazel

from bazel_util.runfiles import get_required_path
from props.db.database import Database
from props.db.models import Snapshot
from props.db.sync.sync import SpecimenBundle, sync_all

pytestmark = pytest.mark.integration


@pytest.fixture
def synced_db(db: Database) -> Generator[Database]:
    """Function-scoped database synced with the specimen from bundle artifacts."""
    slug = os.environ["SPECIMEN_SLUG"]
    code_tar_rloc = os.environ["SPECIMEN_CODE_TAR"]
    data_yaml_rloc = os.environ["SPECIMEN_DATA_YAML"]

    # Resolve runfiles paths
    code_tar = get_required_path(f"_main/{code_tar_rloc}")
    data_yaml = get_required_path(f"_main/{data_yaml_rloc}")

    # Create specimen bundle
    bundle = SpecimenBundle(slug=slug, code_tar=code_tar, data_yaml=data_yaml)

    # Sync from bundle artifacts (no filesystem needed)
    with db.session() as session:
        sync_all(session, specimen_bundles=[bundle])

    return db


def test_specimen(synced_db: Database) -> None:
    """Verify specimen syncs successfully and has expected content."""
    slug = os.environ["SPECIMEN_SLUG"]

    with synced_db.session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).one_or_none()
        assert snapshot is not None, f"Specimen {slug} was not synced"
        assert snapshot.content is not None, f"Specimen {slug} has no content tar"

        tp_count = len(snapshot.true_positives)
        assert tp_count > 0, f"Specimen {slug} has no true positives"


if __name__ == "__main__":
    pytest_bazel.main()
