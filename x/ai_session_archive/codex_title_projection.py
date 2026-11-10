"""Resumable, bounded initialization of the incrementally maintained title candidates."""

import logging
from collections.abc import Callable

from sqlalchemy import select, text, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from x.ai_session_archive.database import (
    CodexTitleBackfillRow,
    CodexTitleCandidateRow,
    CodexTitleObservationRow,
    ResticSnapshotRow,
)

logger = logging.getLogger(__name__)


def backfill_codex_title_candidates_batch(
    session_factory: Callable[[], Session], machine_id: str, *, batch_size: int = 2000
) -> bool:
    """Commit at most one primary-key page; return whether initialization is durable."""
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    with session_factory() as db, db.begin():
        db.execute(
            insert(CodexTitleBackfillRow)
            .values(machine_id=machine_id, latest_snapshot_seeded=False, complete=False)
            .on_conflict_do_nothing()
        )
        state = db.get(CodexTitleBackfillRow, machine_id, with_for_update=True)
        assert state is not None
        if state.complete:
            return True
        page = select(CodexTitleObservationRow.snapshot_id, CodexTitleObservationRow.native_session_id).where(
            CodexTitleObservationRow.machine_id == machine_id
        )
        if not state.latest_snapshot_seeded:
            if state.snapshot_id is None:
                state.snapshot_id = db.scalar(
                    select(ResticSnapshotRow.snapshot_id)
                    .where(ResticSnapshotRow.machine_id == machine_id)
                    .order_by(ResticSnapshotRow.captured_at.desc(), ResticSnapshotRow.snapshot_id.desc())
                    .limit(1)
                )
            page = page.where(CodexTitleObservationRow.snapshot_id == state.snapshot_id)
            if state.native_session_id is not None:
                page = page.where(CodexTitleObservationRow.native_session_id > state.native_session_id)
        elif state.snapshot_id is not None:
            page = page.where(
                tuple_(CodexTitleObservationRow.snapshot_id, CodexTitleObservationRow.native_session_id)
                > (state.snapshot_id, state.native_session_id)
            )
        keys = list(
            db.execute(
                page.order_by(CodexTitleObservationRow.snapshot_id, CodexTitleObservationRow.native_session_id).limit(
                    batch_size
                )
            ).tuples()
        )
        if not keys:
            if not state.latest_snapshot_seeded:
                state.latest_snapshot_seeded = True
                state.snapshot_id = None
                state.native_session_id = None
                return False
            state.complete = True
            return True

        native_ids = sorted({native for _, native in keys})
        db.execute(text("SELECT archive_lock_codex_title(:machine)"), {"machine": machine_id})
        # The first page read can race an old importer. Reread after the same locks
        # its AFTER trigger takes: its correction either precedes this read or follows
        # our commit. Inserts behind the cursor are covered by that trigger too.
        observations = db.execute(
            select(CodexTitleObservationRow, ResticSnapshotRow.captured_at)
            .join(
                ResticSnapshotRow,
                (ResticSnapshotRow.machine_id == CodexTitleObservationRow.machine_id)
                & (ResticSnapshotRow.snapshot_id == CodexTitleObservationRow.snapshot_id),
            )
            .where(
                CodexTitleObservationRow.machine_id == machine_id,
                tuple_(CodexTitleObservationRow.snapshot_id, CodexTitleObservationRow.native_session_id).in_(keys),
            )
            .execution_options(populate_existing=True)
        )
        candidates = {
            (row.native_session_id, row.source_path, row.field): row
            for row in db.scalars(
                select(CodexTitleCandidateRow).where(
                    CodexTitleCandidateRow.machine_id == machine_id,
                    CodexTitleCandidateRow.native_session_id.in_(native_ids),
                )
            )
        }
        for observation, captured_at in observations:
            for field in ("title", "session_kind"):
                value = observation.title if field == "title" else observation.session_kind
                if value is None:
                    continue
                key = (observation.native_session_id, observation.source_path, field)
                candidate = candidates.get(key)
                if candidate is None:
                    candidate = CodexTitleCandidateRow(
                        machine_id=machine_id,
                        native_session_id=observation.native_session_id,
                        source_path=observation.source_path,
                        field=field,
                        value=value,
                        captured_at=captured_at,
                        snapshot_id=observation.snapshot_id,
                    )
                    candidates[key] = candidate
                    db.add(candidate)
                elif (captured_at, observation.snapshot_id) > (candidate.captured_at, candidate.snapshot_id):
                    candidate.value = value
                    candidate.captured_at = captured_at
                    candidate.snapshot_id = observation.snapshot_id
        state.snapshot_id, state.native_session_id = keys[-1]
        logger.info("Backfilled Codex title candidates for %s through %s/%s", machine_id, *keys[-1])
        return False


def ensure_codex_title_candidates(session_factory: Callable[[], Session], machine_id: str) -> None:
    while not backfill_codex_title_candidates_batch(session_factory, machine_id):
        pass
