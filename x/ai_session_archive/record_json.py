"""Shared decoder and scalar projections for stored JSONL records."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ParsedRecordJson:
    parsed: Any
    timestamp: datetime | None
    kind: str | None


@dataclass(frozen=True)
class RecordJsonParseError:
    parse_error: str


def parse_record_json(content: bytes) -> ParsedRecordJson | RecordJsonParseError:
    if content.endswith(b"\n"):
        content = content[:-1]
    if content.endswith(b"\r"):
        content = content[:-1]
    try:
        text = content.decode("utf-8", errors="strict")
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        return RecordJsonParseError(f"{type(exc).__name__}: {exc}"[:1000])

    timestamp = _record_timestamp(parsed) if isinstance(parsed, dict) else None
    kind = _record_kind(parsed) if isinstance(parsed, dict) else None
    return ParsedRecordJson(parsed, timestamp, kind)


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"invalid JSON numeric constant {token}")


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "created_at", "createdAt", "ts"):
        value = record.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
            except ValueError:
                continue
    return None


def _record_kind(record: dict[str, Any]) -> str | None:
    for key in ("type", "kind", "event_type"):
        value = record.get(key)
        if isinstance(value, str):
            return value[:200]
    return None
