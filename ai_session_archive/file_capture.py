"""Capture exact Restic file bytes and parse transcript record batches."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, BinaryIO, cast

from ai_session_archive.models import Harness
from ai_session_archive.record_interpretation import extract_session_metadata
from ai_session_archive.record_json import ParsedRecordJson, parse_record_json
from ai_session_archive.record_payloads import ParsedRecord
from ai_session_archive.restic import ResticRepository
from ai_session_archive.usage_observations import usage_observations_for_record

RECORD_BATCH_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class CapturedFile:
    contents: BinaryIO
    sha256: str
    byte_size: int
    record_count: int


@contextmanager
def capture_file(restic: ResticRepository, snapshot_id: str, path: str) -> Iterator[CapturedFile]:
    with cast(BinaryIO, tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")) as contents:
        restic.dump_to(snapshot_id, path, contents)
        contents.seek(0)
        sha256, byte_size, record_count = fingerprint_file(contents)
        contents.seek(0)
        yield CapturedFile(contents=contents, sha256=sha256, byte_size=byte_size, record_count=record_count)


def fingerprint_file(source: BinaryIO) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    byte_size = 0
    newline_count = 0
    final_byte = b""
    while chunk := source.read(1024 * 1024):
        digest.update(chunk)
        byte_size += len(chunk)
        newline_count += chunk.count(b"\n")
        final_byte = chunk[-1:]
    record_count = newline_count + (1 if byte_size and final_byte != b"\n" else 0)
    return digest.hexdigest(), byte_size, record_count


def read_record_batch(
    source: BinaryIO, metadata: dict[str, Any], *, limit: int, harness: Harness
) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    batch_bytes = 0
    byte_offset = int(metadata.get("_byte_offset", 0))
    while len(records) < limit and batch_bytes < RECORD_BATCH_MAX_BYTES and (raw_bytes := source.readline()):
        parsed = parse_record_json(raw_bytes)
        if isinstance(parsed, ParsedRecordJson) and isinstance(parsed.parsed, Mapping):
            extract_session_metadata(parsed.parsed, metadata, timestamp=parsed.timestamp, harness=harness)
        records.append(
            ParsedRecord(
                raw_bytes=raw_bytes,
                byte_offset=byte_offset,
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                json=parsed,
                usage=tuple(usage_observations_for_record(parsed.parsed))
                if isinstance(parsed, ParsedRecordJson)
                else (),
            )
        )
        byte_offset += len(raw_bytes)
        batch_bytes += len(raw_bytes)
    metadata["_byte_offset"] = byte_offset
    return records
