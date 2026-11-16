"""Backfill missing session kinds from the first effective transcript record."""

from collections.abc import Callable, Mapping

from sqlalchemy import and_, literal, select
from sqlalchemy.orm import Session as DbSession, aliased

from ai_session_archive.database import FileVersionRecordRow, FileVersionRow, RecordPayloadRow, SessionRow
from ai_session_archive.models import Harness, SessionKind
from ai_session_archive.record_interpretation import session_kind_from_record
from ai_session_archive.record_json import ParsedRecordJson, parse_record_json
from ai_session_archive.version_lineage import CHECKPOINT_INTERVAL


def project_missing_session_kinds(session_factory: Callable[[], DbSession], machine_id: str) -> None:
    with session_factory() as db, db.begin():
        latest_version_id = (
            select(FileVersionRow.id)
            .where(FileVersionRow.session_id == SessionRow.id)
            .order_by(FileVersionRow.last_seen_at.desc(), FileVersionRow.id.desc())
            .limit(1)
            .scalar_subquery()
        )

        missing_sessions = (
            select(SessionRow.id.label("session_id"), latest_version_id.label("latest_version_id"))
            .where(SessionRow.machine_id == machine_id, SessionRow.session_kind.is_(None))
            .cte("missing_session_kinds")
        )
        lineage = (
            select(
                missing_sessions.c.session_id,
                FileVersionRow.id.label("version_id"),
                FileVersionRow.parent_version_id,
                FileVersionRow.shared_prefix_record_count,
                literal(0).label("depth"),
            )
            .select_from(missing_sessions)
            .join(FileVersionRow, FileVersionRow.id == missing_sessions.c.latest_version_id)
            .cte("session_kind_version_lineage", recursive=True)
        )
        parent_version = aliased(FileVersionRow)
        lineage = lineage.union_all(
            select(
                lineage.c.session_id,
                parent_version.id,
                parent_version.parent_version_id,
                parent_version.shared_prefix_record_count,
                lineage.c.depth + 1,
            )
            .join(parent_version, parent_version.id == lineage.c.parent_version_id)
            .where(lineage.c.shared_prefix_record_count > 0, lineage.c.depth < CHECKPOINT_INTERVAL - 1)
        )
        first_payloads = (
            select(lineage.c.session_id, FileVersionRecordRow.payload_id)
            .select_from(lineage)
            .join(
                FileVersionRecordRow,
                and_(FileVersionRecordRow.file_version_id == lineage.c.version_id, FileVersionRecordRow.ordinal == 0),
            )
            .where(lineage.c.shared_prefix_record_count == 0)
            .cte("session_kind_first_payloads")
        )
        rows = db.execute(
            select(SessionRow, RecordPayloadRow.raw_bytes)
            .select_from(SessionRow)
            .outerjoin(first_payloads, first_payloads.c.session_id == SessionRow.id)
            .outerjoin(RecordPayloadRow, RecordPayloadRow.id == first_payloads.c.payload_id)
            .where(SessionRow.machine_id == machine_id, SessionRow.session_kind.is_(None))
            .order_by(SessionRow.id)
        )
        for row, raw_bytes in rows.yield_per(32):
            kind = None
            if raw_bytes is not None:
                parsed = parse_record_json(raw_bytes)
                if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
                    kind = session_kind_from_record(Harness(row.harness), parsed.parsed)
            if kind is None and row.harness == Harness.CLAUDE.value and row.parent_link_kind == "claude_path_inferred":
                kind = SessionKind.SUBAGENT
            row.session_kind = (kind or SessionKind.UNKNOWN).value
