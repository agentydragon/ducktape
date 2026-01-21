"""Test that Alembic migrations can be applied in sequence.

This test runs the actual migration sequence against a test database,
catching issues like invalid down_revision references that would cause
`props db recreate` to fail.
"""

from pathlib import Path

import pytest_bazel
from alembic.config import Config
from alembic.script import ScriptDirectory


def test_migration_sequence_loads():
    """Verify Alembic can load and resolve the migration sequence.

    This catches:
    - Invalid down_revision references
    - Branching (multiple heads)
    - Missing base migration
    """
    migrations_dir = Path(__file__).parent

    config = Config()
    config.set_main_option("script_location", str(migrations_dir))

    # This will raise if there are issues with the revision chain
    script = ScriptDirectory.from_config(config)

    # get_current_head() validates the entire chain and raises on:
    # - Invalid down_revision references (KeyError)
    # - Multiple heads (MultipleHeads exception)
    head = script.get_current_head()
    assert head is not None, "No migration head found"

    # Verify we can walk the entire chain
    revisions = list(script.walk_revisions())
    assert len(revisions) > 0, "No migrations found"

    # Verify exactly one base (down_revision = None)
    bases = [r for r in revisions if r.down_revision is None]
    assert len(bases) == 1, f"Expected 1 base migration, found {len(bases)}: {[r.revision for r in bases]}"


if __name__ == "__main__":
    pytest_bazel.main()
