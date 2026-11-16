"""Content-addressed raw record and usage-observation storage."""

from dataclasses import asdict, dataclass
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DbSession, load_only

from ai_session_archive.database import RecordPayloadRow, UsageObservationRow
from ai_session_archive.record_json import ParsedRecordJson, RecordJsonParseError
from ai_session_archive.usage_observations import UsageObservation

PARSER_VERSION = "jsonl-v1"


@dataclass(frozen=True)
class ParsedRecord:
    raw_bytes: bytes
    byte_offset: int
    sha256: str
    json: ParsedRecordJson | RecordJsonParseError
    usage: tuple[UsageObservation, ...]


def store_payloads(db: DbSession, records: list[ParsedRecord]) -> list[RecordPayloadRow]:
    by_hash: dict[str, ParsedRecord] = {}
    for record in records:
        if record.sha256 in by_hash and by_hash[record.sha256].raw_bytes != record.raw_bytes:
            raise RuntimeError("record SHA-256 collision detected")
        by_hash[record.sha256] = record
    new_ids = set(
        db.scalars(
            pg_insert(RecordPayloadRow)
            .values(
                [
                    {
                        "id": uuid5(NAMESPACE_URL, f"ai-session-record:{PARSER_VERSION}:{record.sha256}"),
                        "sha256": record.sha256,
                        "parser_version": PARSER_VERSION,
                        "raw_bytes": record.raw_bytes,
                        "parse_error": record.json.parse_error
                        if isinstance(record.json, RecordJsonParseError)
                        else None,
                        "timestamp": record.json.timestamp if isinstance(record.json, ParsedRecordJson) else None,
                        "kind": record.json.kind if isinstance(record.json, ParsedRecordJson) else None,
                    }
                    for record in sorted(by_hash.values(), key=lambda record: record.sha256)
                ]
            )
            .on_conflict_do_nothing()
            .returning(RecordPayloadRow.id)
        )
    )
    payload_by_hash = {
        row.sha256: row
        for row in db.scalars(
            select(RecordPayloadRow)
            .options(load_only(RecordPayloadRow.sha256, RecordPayloadRow.raw_bytes))
            .where(RecordPayloadRow.sha256.in_(by_hash), RecordPayloadRow.parser_version == PARSER_VERSION)
        )
    }
    for record in by_hash.values():
        payload = payload_by_hash[record.sha256]
        if payload.raw_bytes != record.raw_bytes:
            raise RuntimeError("record SHA-256 collision detected")
        if payload.id in new_ids:
            for index, observation in enumerate(record.usage):
                db.add(UsageObservationRow(payload_id=payload.id, observation_index=index, **asdict(observation)))
    db.flush()
    return [payload_by_hash[record.sha256] for record in records]
