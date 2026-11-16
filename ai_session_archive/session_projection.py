"""Project captured transcript metadata onto session rows."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ai_session_archive.claude_titles import ClaudeTitle
from ai_session_archive.database import CodexTitleCandidateRow, SessionRow
from ai_session_archive.models import Harness, SessionKind

CLAUDE_TITLE_PROJECTION_VERSION = 1


def update_session_metadata(db: DbSession, row: SessionRow, metadata: Mapping[str, Any]) -> None:
    native_session_id = metadata.get("native_session_id")
    if row.harness == Harness.CODEX.value:
        candidates = set(row.native_session_id_candidates)
        if row.native_session_id and not row.native_session_id_inferred:
            candidates.add(row.native_session_id)
        metadata_candidates = metadata.get("native_session_id_candidates", [])
        if isinstance(metadata_candidates, list):
            candidates.update(item for item in metadata_candidates if isinstance(item, str) and item)
        if isinstance(native_session_id, str) and native_session_id:
            candidates.add(native_session_id)
        if len(candidates) > 1:
            row.native_session_id_candidates = sorted(candidates)
            row.native_session_id = None
            row.native_session_id_inferred = False
            row.native_session_id_conflict = True
            for candidate in candidates:
                _resolve_codex_children(db, row.machine_id, candidate)
        elif candidates and not row.native_session_id_conflict:
            row.native_session_id = next(iter(candidates))
            row.native_session_id_candidates = list(candidates)
            row.native_session_id_inferred = False
            _resolve_codex_children(db, row.machine_id, row.native_session_id)
    if row.harness == Harness.CODEX.value:
        parent_candidates = set(row.parent_native_session_id_candidates)
        if row.parent_native_session_id:
            parent_candidates.add(row.parent_native_session_id)
        metadata_parent_candidates = metadata.get("parent_native_session_id_candidates", [])
        if isinstance(metadata_parent_candidates, list):
            parent_candidates.update(item for item in metadata_parent_candidates if isinstance(item, str) and item)
        parent_native_session_id = metadata.get("parent_native_session_id")
        if isinstance(parent_native_session_id, str) and parent_native_session_id:
            parent_candidates.add(parent_native_session_id)
        if len(parent_candidates) > 1:
            row.parent_native_session_id_candidates = sorted(parent_candidates)
            row.parent_native_session_id = None
            row.parent_native_session_id_conflict = True
            row.parent_session_id = None
            row.parent_link_kind = "codex_parent_thread_id_inconsistent"
        elif parent_candidates and not row.parent_native_session_id_conflict:
            row.parent_native_session_id = next(iter(parent_candidates))
            row.parent_native_session_id_candidates = list(parent_candidates)
            _resolve_codex_parent_link(db, row)
    if row.cwd is None and metadata.get("cwd"):
        row.cwd = str(metadata["cwd"])
    session_kind = metadata.get("session_kind")
    if (
        isinstance(session_kind, str)
        and session_kind in {kind.value for kind in SessionKind}
        and (session_kind != SessionKind.UNKNOWN.value or row.session_kind is None)
    ):
        row.session_kind = session_kind
    # An unchanged known version returns no metadata; a parsed version replaces the projection.
    if row.harness == Harness.CLAUDE.value and metadata:
        title: ClaudeTitle | None = metadata.get("claude_title")
        row.title = title.text if title is not None else None
        row.claude_title_projection_version = CLAUDE_TITLE_PROJECTION_VERSION
    if row.harness == Harness.CODEX.value:
        _apply_latest_codex_title(db, row)
    first = metadata.get("first_timestamp")
    last = metadata.get("last_timestamp")
    if isinstance(first, datetime) and isinstance(last, datetime):
        row.started_at = first if row.started_at is None else min(row.started_at, first)
        row.last_activity_at = last if row.last_activity_at is None else max(row.last_activity_at, last)


def _apply_latest_codex_title(db: DbSession, row: SessionRow) -> None:
    if row.native_session_id_conflict or not row.native_session_id:
        return
    candidates = dict(
        db.execute(
            select(CodexTitleCandidateRow.field, CodexTitleCandidateRow.value).where(
                CodexTitleCandidateRow.machine_id == row.machine_id,
                CodexTitleCandidateRow.native_session_id == row.native_session_id,
                CodexTitleCandidateRow.source_path == row.source_path,
            )
        )
        .tuples()
        .all()
    )
    title = candidates.get("title")
    kind = candidates.get("session_kind")
    if title is not None:
        row.title = title
        row.codex_title_from_metadata = True
    if kind is not None:
        row.session_kind = kind


def _resolve_codex_parent_link(db: DbSession, child: SessionRow) -> None:
    if child.parent_native_session_id_conflict:
        child.parent_session_id = None
        child.parent_link_kind = "codex_parent_thread_id_inconsistent"
        return
    parent_native_session_id = child.parent_native_session_id
    if not parent_native_session_id:
        return
    conflicting_parents = db.scalars(
        select(SessionRow).where(
            SessionRow.machine_id == child.machine_id,
            SessionRow.harness == Harness.CODEX.value,
            SessionRow.native_session_id_conflict.is_(True),
        )
    )
    if any(parent_native_session_id in parent.native_session_id_candidates for parent in conflicting_parents):
        child.parent_session_id = None
        child.parent_link_kind = "codex_parent_thread_id_inconsistent"
        return
    parents = list(
        db.scalars(
            select(SessionRow).where(
                SessionRow.machine_id == child.machine_id,
                SessionRow.harness == Harness.CODEX.value,
                SessionRow.native_session_id == parent_native_session_id,
                SessionRow.native_session_id_inferred.is_(False),
                SessionRow.identity_inferred.is_(False),
                SessionRow.id != child.id,
            )
        )
    )
    if len(parents) == 1:
        child.parent_session_id = parents[0].id
        child.parent_link_kind = "codex_parent_thread_id"
    else:
        child.parent_session_id = None
        suffix = "unresolved" if not parents else "ambiguous"
        child.parent_link_kind = f"codex_parent_thread_id_{suffix}"


def _resolve_codex_children(db: DbSession, machine_id: str, native_session_id: str) -> None:
    children = db.scalars(
        select(SessionRow).where(
            SessionRow.machine_id == machine_id,
            SessionRow.harness == Harness.CODEX.value,
            SessionRow.parent_native_session_id == native_session_id,
        )
    )
    for child in children:
        _resolve_codex_parent_link(db, child)
