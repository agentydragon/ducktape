"""Interpret Claude and Codex transcript records into session metadata."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ai_session_archive.claude_titles import select_claude_title
from ai_session_archive.codex_metadata import classify_codex_source
from ai_session_archive.models import Harness, SessionKind


def extract_session_metadata(
    record: Mapping[str, Any], metadata: dict[str, Any], *, timestamp: datetime | None, harness: Harness
) -> None:
    payload = record.get("payload")
    data = payload if isinstance(payload, dict) else {}
    if record.get("type") == "session_meta":
        if isinstance(data.get("id"), str):
            remember_metadata_value(metadata, "native_session_id", data["id"])
        if isinstance(data.get("parent_thread_id"), str):
            remember_metadata_value(metadata, "parent_native_session_id", data["parent_thread_id"])
        if harness == Harness.CODEX:
            remember_session_kind(metadata, classify_codex_source(data.get("source")))
    if harness == Harness.CLAUDE and isinstance(record.get("isSidechain"), bool):
        remember_session_kind(metadata, SessionKind.SUBAGENT if record["isSidechain"] else SessionKind.MAIN)
    for candidate in (record.get("cwd"), data.get("cwd")):
        if isinstance(candidate, str):
            metadata.setdefault("cwd", candidate)
            break
    if timestamp is not None:
        first = metadata.get("first_timestamp")
        last = metadata.get("last_timestamp")
        metadata["first_timestamp"] = timestamp if first is None else min(first, timestamp)
        metadata["last_timestamp"] = timestamp if last is None else max(last, timestamp)
    metadata["claude_title"] = select_claude_title(metadata.get("claude_title"), record)


def session_kind_from_record(harness: Harness, record: Mapping[str, Any]) -> SessionKind | None:
    if harness == Harness.CODEX and record.get("type") == "session_meta":
        payload = record.get("payload")
        data = payload if isinstance(payload, Mapping) else {}
        return classify_codex_source(data.get("source"))
    if harness == Harness.CLAUDE and isinstance(record.get("isSidechain"), bool):
        return SessionKind.SUBAGENT if record["isSidechain"] else SessionKind.MAIN
    return None


def remember_session_kind(metadata: dict[str, Any], kind: SessionKind) -> None:
    if metadata.get("session_kind_conflict"):
        return
    existing = metadata.get("session_kind")
    if kind == SessionKind.UNKNOWN:
        metadata.setdefault("session_kind", SessionKind.UNKNOWN.value)
        return
    if existing is None or existing == SessionKind.UNKNOWN.value:
        metadata["session_kind"] = kind.value
    elif existing != kind.value:
        metadata["session_kind"] = SessionKind.UNKNOWN.value
        metadata["session_kind_conflict"] = True


def remember_metadata_value(metadata: dict[str, Any], key: str, value: str) -> None:
    candidates = metadata.setdefault(f"{key}_candidates", [])
    if value not in candidates:
        candidates.append(value)
    if len(candidates) > 1:
        metadata[f"{key}_conflict"] = True
    metadata.setdefault(key, value)
