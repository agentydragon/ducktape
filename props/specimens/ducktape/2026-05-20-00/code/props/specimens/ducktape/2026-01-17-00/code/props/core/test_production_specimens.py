"""Production specimens repository validation.

This module contains data quality checks for the production specimens repository.
These tests validate the external specimens repo (ADGN_PROPS_SPECIMENS_ROOT), not
the git-tracked test fixtures.

Most schema/format validations are enforced by the database during sync.
These tests check higher-level data quality requirements that can't be expressed
as DB constraints (minimum issue counts per split, minimum specimens per split).

TODO: Move these checks to `props specimens check` CLI command
so they can run in the specimens repo CI, not here.

Run with: pytest -m requires_production_specimens
Skip with: pytest -m 'not requires_production_specimens'
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from hamcrest import assert_that, greater_than_or_equal_to
from sqlalchemy import create_engine, text

from props.core.db.config import DatabaseConfig, get_database_config
from props.core.db.models import Snapshot
from props.core.db.session import dispose_db, get_session, init_db, recreate_database
from props.core.db.setup import ensure_database_exists
from props.core.db.sync.sync import sync_all
from props.core.splits import Split

pytestmark = [pytest.mark.requires_production_specimens, pytest.mark.integration]


# =============================================================================
# Production specimens database fixture (self-contained in this module)
# =============================================================================


@pytest.fixture(scope="module")
def module_monkeypatch() -> Generator[pytest.MonkeyPatch]:
    """Module-scoped monkeypatch for environment variable overrides."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synced_production_db(module_monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[DatabaseConfig]:
    """Module-scoped synced production database.

    Creates a single database for all production specimen tests in this module.
    Syncs ONCE at module start - tests share the same synced data.
    """
    db_name = "props_test_production_specimens"
    base_config = get_database_config()
    ensure_database_exists(base_config, db_name, drop_existing=True)
    test_config = base_config.with_database(db_name)

    # Keep postgres engine for teardown
    postgres_config = base_config.with_database("postgres")
    postgres_engine = create_engine(postgres_config.admin_url(), isolation_level="AUTOCOMMIT")

    dispose_db()
    init_db(test_config)
    recreate_database()

    # Sync PRODUCTION specimens (uses ADGN_PROPS_SPECIMENS_ROOT from env)
    with get_session() as session:
        sync_all(session, use_staged=True)

    yield test_config

    # Cleanup at end of module
    dispose_db()
    with postgres_engine.connect() as conn:
        conn.execute(
            text(
                f"""
            SELECT pg_terminate_backend(pg_stat_activity.pid)
            FROM pg_stat_activity
            WHERE pg_stat_activity.datname = '{db_name}'
              AND pid <> pg_backend_pid()
        """
            )
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    postgres_engine.dispose()


# =============================================================================
# Data quality checks
# =============================================================================


def test_split_distribution_and_issue_counts(synced_production_db: DatabaseConfig) -> None:
    """Verify train/valid/test split distribution meets minimum requirements.

    Checks:
    1. Each split has at least one specimen
    2. VALID has at least 50 issues (for statistically meaningful validation)
    3. TRAIN has at least 60 issues (enough training data)
    4. TEST has at least 60 issues (when enabled)

    TODO: Move to `props specimens check` CLI command.
    """
    with get_session() as session:
        snapshots = session.query(Snapshot).all()

        # Count specimens per split
        specimen_counts: Counter[Split] = Counter()
        issue_counts: Counter[Split] = Counter()

        for snapshot in snapshots:
            specimen_counts[snapshot.split] += 1
            issue_counts[snapshot.split] += len(snapshot.true_positives)

    # Each split should have at least one specimen
    assert_that(specimen_counts[Split.TRAIN], greater_than_or_equal_to(1))
    assert_that(specimen_counts[Split.VALID], greater_than_or_equal_to(1))
    # TODO: Uncomment when TEST split is populated
    # assert_that(specimen_counts[Split.TEST], greater_than_or_equal_to(1))

    # Issue count constraints for statistically meaningful evaluation
    assert_that(issue_counts[Split.TRAIN], greater_than_or_equal_to(60))
    assert_that(issue_counts[Split.VALID], greater_than_or_equal_to(50))
    # TODO: Uncomment when TEST split is populated
    # assert_that(issue_counts[Split.TEST], greater_than_or_equal_to(60))
