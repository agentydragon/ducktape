"""Restore JSONB-incompatible but valid JSON payloads while all writers are quiescent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from x.ai_session_archive.database import (
    FileVersionRecordRow,
    FileVersionRow,
    RecordPayloadRow,
    SnapshotFileRow,
    make_engine,
    make_session_factory,
)
from x.ai_session_archive.ingest import CHECKPOINT_INTERVAL, _parse_record_content

_BATCH_LIMIT = 1000
_VERSION_LIMIT = 10000
_SESSION_ID_SAMPLE_LIMIT = 100


@dataclass(frozen=True)
class BackfillReport:
    inspected_payload_count: int
    repaired_payload_count: int
    skipped_payload_count: int
    more_candidates: bool
    recomputed_version_count: int
    updated_snapshot_file_count: int
    metadata_review_session_count: int
    metadata_review_session_ids: list[str]
    metadata_review_session_ids_truncated: bool


def backfill_jsonb_payloads(
    session_factory: Any, *, limit: int = 100, max_versions: int = _VERSION_LIMIT
) -> BackfillReport:
    if not 1 <= limit <= _BATCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {_BATCH_LIMIT}")
    if not 1 <= max_versions <= _VERSION_LIMIT:
        raise ValueError(f"max_versions must be between 1 and {_VERSION_LIMIT}")

    with session_factory() as db, db.begin():
        candidates = list(
            db.scalars(
                select(RecordPayloadRow)
                .where(
                    RecordPayloadRow.parsed_from_raw.is_(False),
                    RecordPayloadRow.parse_error.is_not(None),
                    or_(
                        RecordPayloadRow.parse_error.like("ValueError: JSONB cannot store %"),
                        RecordPayloadRow.parse_error.like("ValueError: JSON nesting exceeds Postgres safety limit %"),
                        RecordPayloadRow.parse_error.like(
                            "ValueError: Exceeds the limit (% for integer string conversion:%"
                        ),
                    ),
                )
                .order_by(RecordPayloadRow.id)
                .limit(limit + 1)
                .with_for_update(skip_locked=True)
            )
        )
        more_candidates = len(candidates) > limit
        candidates = candidates[:limit]
        repaired: list[RecordPayloadRow] = []
        skipped_count = 0
        for payload in candidates:
            if hashlib.sha256(payload.raw_bytes).hexdigest() != payload.sha256:
                raise RuntimeError(f"record payload {payload.id} no longer matches its stored SHA-256")
            parsed = _parse_record_content(_record_content(payload.raw_bytes))
            if parsed.parse_error is not None or not parsed.parsed_from_raw:
                skipped_count += 1
                continue
            payload.parsed = None
            payload.parsed_from_raw = True
            payload.parse_error = None
            payload.timestamp = parsed.timestamp
            payload.kind = parsed.kind
            repaired.append(payload)

        if not repaired:
            return BackfillReport(len(candidates), 0, skipped_count, more_candidates, 0, 0, 0, [], False)

        payload_ids = [payload.id for payload in repaired]
        direct_version_ids = set(
            db.scalars(
                select(FileVersionRecordRow.file_version_id).where(FileVersionRecordRow.payload_id.in_(payload_ids))
            )
        )
        session_ids = sorted(
            set(db.scalars(select(FileVersionRow.session_id).where(FileVersionRow.id.in_(direct_version_ids)))), key=str
        )
        repaired_occurrences = _repaired_occurrences(db, payload_ids, max_versions=max_versions)
        affected_version_ids = set(repaired_occurrences)
        versions = list(
            db.scalars(
                select(FileVersionRow)
                .where(FileVersionRow.id.in_(affected_version_ids))
                .order_by(FileVersionRow.id)
                .with_for_update()
            )
        )
        updated_version_count = 0
        version_by_id = {row.id: row for row in versions}
        for version in versions:
            issue_count = version.parse_issue_count - len(repaired_occurrences[version.id])
            if issue_count < 0:
                raise RuntimeError(
                    f"file version {version.id} has fewer recorded parse issues than repaired record occurrences"
                )
            complete = issue_count == 0
            if version.parse_issue_count != issue_count or version.complete != complete:
                version.parse_issue_count = issue_count
                version.complete = complete
                updated_version_count += 1

        updated_file_count = 0
        if version_by_id:
            snapshot_files = db.scalars(
                select(SnapshotFileRow).where(
                    SnapshotFileRow.file_version_id.in_(version_by_id),
                    SnapshotFileRow.status.in_(("complete", "partial")),
                )
            )
            for snapshot_file in snapshot_files:
                version = version_by_id[snapshot_file.file_version_id]
                status = "complete" if version.complete else "partial"
                if snapshot_file.status != status:
                    snapshot_file.status = status
                    updated_file_count += 1

        session_id_strings = [str(session_id) for session_id in session_ids]
        return BackfillReport(
            inspected_payload_count=len(candidates),
            repaired_payload_count=len(repaired),
            skipped_payload_count=skipped_count,
            more_candidates=more_candidates,
            recomputed_version_count=updated_version_count,
            updated_snapshot_file_count=updated_file_count,
            metadata_review_session_count=len(session_id_strings),
            metadata_review_session_ids=session_id_strings[:_SESSION_ID_SAMPLE_LIMIT],
            metadata_review_session_ids_truncated=len(session_id_strings) > _SESSION_ID_SAMPLE_LIMIT,
        )


def _record_content(raw_bytes: bytes) -> bytes:
    content = raw_bytes[:-1] if raw_bytes.endswith(b"\n") else raw_bytes
    return content[:-1] if content.endswith(b"\r") else content


def _repaired_occurrences(db: DbSession, payload_ids: list[UUID], *, max_versions: int) -> dict[UUID, set[int]]:
    occurrences: dict[UUID, set[int]] = defaultdict(set)
    direct_references = db.execute(
        select(FileVersionRecordRow.file_version_id, FileVersionRecordRow.ordinal).where(
            FileVersionRecordRow.payload_id.in_(payload_ids)
        )
    )
    for version_id, ordinal in direct_references:
        occurrences[version_id].add(ordinal)

    affected = set(occurrences)
    if len(affected) > max_versions:
        raise RuntimeError(
            f"repair reaches more than {max_versions} file versions; lower --limit or raise --max-versions"
        )
    frontier = set(occurrences)
    for _ in range(CHECKPOINT_INTERVAL):
        if not frontier:
            break
        children = db.execute(
            select(
                FileVersionRow.id, FileVersionRow.parent_version_id, FileVersionRow.shared_prefix_record_count
            ).where(FileVersionRow.parent_version_id.in_(frontier))
        )
        next_frontier = set()
        for child_id, parent_id, shared_prefix_record_count in children:
            if parent_id is None:
                raise RuntimeError(f"file version {child_id} has no parent in a descendant query")
            inherited = {ordinal for ordinal in occurrences[parent_id] if ordinal < shared_prefix_record_count}
            if not inherited:
                continue
            occurrences[child_id].update(inherited)
            next_frontier.add(child_id)
        affected.update(next_frontier)
        if len(affected) > max_versions:
            raise RuntimeError(
                f"repair reaches more than {max_versions} file versions; lower --limit or raise --max-versions"
            )
        frontier = next_frontier
    if frontier:
        raise RuntimeError("file version ancestry exceeds the configured checkpoint interval")
    return dict(occurrences)


def _parse_args(arguments: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--writers-drained",
        action="store_true",
        required=True,
        help="assert that every archive importer is quiescent for the duration of this repair",
    )
    parser.add_argument("--limit", type=int, default=100, help=f"maximum payloads per run (1-{_BATCH_LIMIT})")
    parser.add_argument(
        "--max-versions",
        type=int,
        default=_VERSION_LIMIT,
        help=f"maximum affected versions per run (1-{_VERSION_LIMIT})",
    )
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> None:
    args = _parse_args(arguments)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL must configure the Postgres connection")
    engine = make_engine(database_url)
    try:
        report = backfill_jsonb_payloads(make_session_factory(engine), limit=args.limit, max_versions=args.max_versions)
        print(json.dumps(asdict(report), sort_keys=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
