"""Debug test with comprehensive SQLAlchemy event logging."""

import logging
import traceback

import pytest_bazel
from sqlalchemy import event
from sqlalchemy.orm import Session

from props.db.database import Database
from props.db.models import FileSet, FileSetMember

logger = logging.getLogger(__name__)


def setup_comprehensive_logging(engine):
    """Add event listeners to log ALL SQLAlchemy operations."""

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def receive_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        # Log ALL SQL statements, including database-level operations
        if "DELETE" in statement.upper() or "file_set_member" in statement.lower():
            logger.error(f"[SQL] {statement}")
            logger.error(f"[SQL] params: {parameters}")
        return statement, parameters

    @event.listens_for(engine, "after_cursor_execute")
    def receive_after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        # Log row counts for DELETE statements
        if "DELETE" in statement.upper() or "file_set_member" in statement.lower():
            logger.error(f"[SQL] rowcount: {cursor.rowcount}")

    @event.listens_for(Session, "after_flush", propagate=True)
    def receive_after_flush(session, flush_context):
        logger.warning(
            f"[EVENT] after_flush: {len(session.new)} new, {len(session.dirty)} dirty, {len(session.deleted)} deleted"
        )
        for obj in session.new:
            logger.warning(f"[EVENT]   NEW: {obj.__class__.__name__} {obj}")
        for obj in session.deleted:
            logger.warning(f"[EVENT]   DELETED: {obj.__class__.__name__} {obj}")

    @event.listens_for(Session, "after_commit", propagate=True)
    def receive_after_commit(session):
        logger.warning("[EVENT] after_commit")

    @event.listens_for(Session, "after_rollback", propagate=True)
    def receive_after_rollback(session):
        logger.warning("[EVENT] after_rollback")

    @event.listens_for(Session, "before_flush", propagate=True)
    def receive_before_flush(session, flush_context, instances):
        logger.warning(
            f"[EVENT] before_flush: {len(session.new)} new, {len(session.dirty)} dirty, {len(session.deleted)} deleted"
        )

    # Track specific model deletions
    @event.listens_for(FileSetMember, "after_delete", propagate=True)
    def receive_after_delete(mapper, connection, target):
        logger.error(
            f"[EVENT] FileSetMember DELETED: slug={target.snapshot_slug} hash={target.files_hash} path={target.file_path}"
        )
        logger.error(f"[EVENT] Delete traceback:\n{''.join(traceback.format_stack())}")

    @event.listens_for(FileSet, "after_delete", propagate=True)
    def receive_fileset_delete(mapper, connection, target):
        logger.error(f"[EVENT] FileSet DELETED: slug={target.snapshot_slug} hash={target.files_hash}")


def test_with_full_event_logging(synced_db: Database):
    """Test with comprehensive event logging to catch FileSetMember deletion."""
    setup_comprehensive_logging(synced_db.engine)

    logger.warning("=" * 80)
    logger.warning("TEST STARTING - Opening new session to query FileSet")
    logger.warning("=" * 80)

    with synced_db.session() as session:
        # Query for the problematic FileSet
        fs = (
            session.query(FileSet)
            .filter_by(snapshot_slug="test-fixtures/train1", files_hash="d5673969af8b94a23a229e9215d473c4")
            .first()
        )

        logger.warning(f"FileSet found: {fs is not None}")

        # Query members
        members = (
            session.query(FileSetMember)
            .filter_by(snapshot_slug="test-fixtures/train1", files_hash="d5673969af8b94a23a229e9215d473c4")
            .all()
        )

        logger.warning(f"Members found: {len(members)}")
        for m in members:
            logger.warning(f"  - {m.file_path}")

        assert fs is not None, "FileSet not found"
        assert len(members) > 0, "FileSet exists but has ZERO members! (should have been logged above if deleted)"


if __name__ == "__main__":
    pytest_bazel.main()
