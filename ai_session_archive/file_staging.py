"""Durably stage parsed transcript batches before version publication."""

from collections.abc import Callable, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import FileVersionImportRecordRow, FileVersionImportRow, FileVersionRow
from ai_session_archive.record_json import RecordJsonParseError
from ai_session_archive.record_payloads import ParsedRecord, store_payloads
from ai_session_archive.version_lineage import effective_payload_ids


class FileStaging:
    def __init__(self, session_factory: Callable[[], DbSession]):
        self._session_factory = session_factory

    def store_record_batch(
        self,
        version_id: UUID,
        start: int,
        records: list[ParsedRecord],
        *,
        parent: FileVersionRow | None,
        parent_ranks: Mapping[UUID, int],
    ) -> None:
        with self._session_factory() as db, db.begin():
            stage = db.get(FileVersionImportRow, version_id, with_for_update=True)
            if stage is None:
                # Another importer published this content after our initial check.
                if db.get(FileVersionRow, version_id) is None:
                    raise RuntimeError(f"missing file import: {version_id}")
                return
            if start > stage.imported_record_count:
                raise RuntimeError(f"file import {version_id} is missing records before {start}")
            payloads = store_payloads(db, records)
            committed = min(len(records), stage.imported_record_count - start)
            if committed:
                inherited = min(committed, max(0, stage.shared_prefix_record_count - start))
                if inherited and (
                    parent is None
                    or effective_payload_ids(db, parent, parent_ranks, start, start + inherited)
                    != [payload.id for payload in payloads[:inherited]]
                ):
                    raise RuntimeError("file content hash collides with staged prefix")
                stored = list(
                    db.execute(
                        select(FileVersionImportRecordRow.payload_id, FileVersionImportRecordRow.byte_offset)
                        .where(
                            FileVersionImportRecordRow.import_id == version_id,
                            FileVersionImportRecordRow.ordinal >= start + inherited,
                            FileVersionImportRecordRow.ordinal < start + committed,
                        )
                        .order_by(FileVersionImportRecordRow.ordinal)
                    ).tuples()
                )
                expected = [
                    (payload.id, record.byte_offset)
                    for record, payload in zip(records[inherited:committed], payloads[inherited:committed], strict=True)
                ]
                if stored != expected:
                    raise RuntimeError("file content hash collides with staged records")
            matching_prefix = parent is not None and stage.shared_prefix_record_count == stage.imported_record_count
            parent_ids = (
                effective_payload_ids(db, parent, parent_ranks, start + committed, start + len(records))
                if matching_prefix and parent is not None
                else []
            )
            for index in range(committed, len(records)):
                record, payload = records[index], payloads[index]
                if isinstance(record.json, RecordJsonParseError):
                    stage.parse_issue_count += 1
                if matching_prefix:
                    parent_index = index - committed
                    if parent_index < len(parent_ids) and parent_ids[parent_index] == payload.id:
                        stage.shared_prefix_record_count += 1
                        continue
                    matching_prefix = False
                db.add(
                    FileVersionImportRecordRow(
                        import_id=version_id,
                        ordinal=start + index,
                        byte_offset=record.byte_offset,
                        payload_id=payload.id,
                    )
                )
            stage.imported_record_count = max(stage.imported_record_count, start + len(records))
