"""Read Codex display names from files captured in a Restic snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from x.ai_session_archive.postgres_text import is_safe_postgres_text
from x.ai_session_archive.restic import ResticCommandError, ResticOutputError, ResticRepository

SQLITE_MANIFEST = PurePosixPath("/.local/state/ducktape/ai-session-backup-sqlite/manifest.json")


@dataclass(frozen=True)
class CodexTitle:
    native_session_id: str
    source_path: str
    title: str
    title_source: str
    updated_at: datetime | None


def _snapshot_file(restic: ResticRepository, snapshot_id: str, path: PurePosixPath) -> bool:
    return any(
        file.path == str(path) and file.file_type == "file" for file in restic.list_files(snapshot_id, str(path))
    )


def _dump(restic: ResticRepository, snapshot_id: str, path: PurePosixPath) -> bytes:
    with tempfile.TemporaryFile(mode="w+b") as output:
        restic.dump_to(snapshot_id, str(path), output)
        output.seek(0)
        return output.read()


def _index_titles(
    restic: ResticRepository, snapshot_id: str, codex_root: PurePosixPath
) -> tuple[dict[str, tuple[str, datetime]], int]:
    path = codex_root / "session_index.jsonl"
    if not _snapshot_file(restic, snapshot_id, path):
        return {}, 0
    rows: dict[str, tuple[str, datetime, int]] = {}
    malformed = 0
    for ordinal, line in enumerate(_dump(restic, snapshot_id, path).splitlines()):
        try:
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("non-object record")
            native_id, title, timestamp = item.get("id"), item.get("thread_name"), item.get("updated_at")
            if (
                not isinstance(native_id, str)
                or not native_id
                or not is_safe_postgres_text(native_id)
                or not isinstance(title, str)
                or not is_safe_postgres_text(title)
                or not isinstance(timestamp, str)
            ):
                raise ValueError("missing id, thread_name, or updated_at")
            updated_at = datetime.fromisoformat(timestamp)
            if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                raise ValueError("timestamp lacks a timezone")
        except ValueError, TypeError:
            malformed += 1
            continue
        previous = rows.get(native_id)
        if previous is None or (updated_at, ordinal) > (previous[1], previous[2]):
            rows[native_id] = (title, updated_at, ordinal)
    return {native_id: (title, timestamp) for native_id, (title, timestamp, _) in rows.items()}, malformed


def _manifest_image(manifest: Any) -> tuple[PurePosixPath, PurePosixPath, str] | None:
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or not isinstance(manifest.get("databases"), list)
    ):
        raise ValueError("invalid SQLite snapshot manifest")
    candidates = []
    for entry in manifest["databases"]:
        if not isinstance(entry, dict) or entry.get("harness") != "codex":
            continue
        source = entry.get("source")
        image = entry.get("image")
        digest = entry.get("sha256")
        if not isinstance(source, str) or not isinstance(image, str) or not isinstance(digest, str):
            raise ValueError("invalid Codex SQLite manifest entry")
        source_path = PurePosixPath(source)
        image_path = PurePosixPath(image)
        if source_path.name != "state_5.sqlite":
            continue
        if not source_path.is_absolute() or image_path.is_absolute() or ".." in image_path.parts:
            raise ValueError("unsafe Codex SQLite manifest path")
        if image_path.parts[:2] != ("images", "codex") or image_path.name != "state_5.sqlite":
            raise ValueError("unexpected Codex SQLite image path")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("invalid Codex SQLite image digest")
        candidates.append((source_path.parent, SQLITE_MANIFEST.parent / image_path, digest))
    if len(candidates) > 1:
        raise ValueError("ambiguous Codex state_5.sqlite images in snapshot manifest")
    return candidates[0] if candidates else None


def _sqlite_titles(
    restic: ResticRepository, snapshot_id: str, codex_root: PurePosixPath
) -> dict[str, tuple[str, str, str]]:
    if not _snapshot_file(restic, snapshot_id, SQLITE_MANIFEST):
        return {}
    manifest = json.loads(_dump(restic, snapshot_id, SQLITE_MANIFEST))
    selected = _manifest_image(manifest)
    if selected is None:
        return {}
    physical_root, image_path, digest = selected
    if not _snapshot_file(restic, snapshot_id, image_path):
        raise ValueError("Codex SQLite image listed in manifest is missing")
    with tempfile.TemporaryDirectory() as scratch:
        image_file = Path(scratch) / "state_5.sqlite"
        with image_file.open("wb") as output:
            restic.dump_to(snapshot_id, str(image_path), output)
        with image_file.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != digest:
                raise ValueError("Codex SQLite image digest disagrees with manifest")
        database = sqlite3.connect(f"file:{image_file}?mode=ro&immutable=1", uri=True)
        try:
            if database.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Codex SQLite image failed quick_check")
            rows = database.execute("SELECT id, rollout_path, name, title FROM threads")
            titles = {}
            for native_id, rollout_path, name, title in rows:
                if (
                    not isinstance(native_id, str)
                    or not isinstance(rollout_path, str)
                    or not is_safe_postgres_text(native_id)
                    or not is_safe_postgres_text(rollout_path)
                ):
                    continue
                try:
                    relative = PurePosixPath(rollout_path).relative_to(physical_root)
                except ValueError:
                    continue
                if relative.parts[:1] not in (("sessions",), ("archived_sessions",)) or relative.suffix != ".jsonl":
                    continue
                value = None
                title_source = None
                for candidate, candidate_source in ((name, "sqlite_name"), (title, "sqlite_title")):
                    if isinstance(candidate, str) and candidate.strip() and is_safe_postgres_text(candidate):
                        value = candidate
                        title_source = candidate_source
                        break
                source_path = str(codex_root / relative)
                if not isinstance(value, str) or title_source is None or not is_safe_postgres_text(source_path):
                    continue
                titles[native_id] = (source_path, value.strip()[:500], title_source)
            return titles
        finally:
            database.close()


def read_codex_titles(
    restic: ResticRepository, snapshot_id: str, codex_roots: tuple[PurePosixPath, ...]
) -> tuple[list[CodexTitle], list[str], list[str]]:
    """Prefer captured session-index names, retaining SQLite paths and title fallbacks."""
    if len(codex_roots) > 1:
        raise ValueError("Codex title import requires one configured archive root per machine")
    if not codex_roots:
        return [], [], []
    root = codex_roots[0]
    warnings: list[str] = []
    retry_errors: list[str] = []
    try:
        indexed, malformed = _index_titles(restic, snapshot_id, root)
        if malformed:
            warnings.append(f"Codex session index has {malformed} malformed records")
    except (ResticCommandError, ResticOutputError, OSError) as exc:
        indexed = {}
        retry_errors.append(f"Codex session index read failed: {type(exc).__name__}: {exc}")
    except (ValueError, TypeError) as exc:
        indexed = {}
        warnings.append(f"Codex session index unavailable: {type(exc).__name__}: {exc}")
    try:
        sqlite = _sqlite_titles(restic, snapshot_id, root)
    except (ResticCommandError, ResticOutputError, OSError) as exc:
        sqlite = {}
        retry_errors.append(f"Codex SQLite image read failed: {type(exc).__name__}: {exc}")
    except (ValueError, TypeError, sqlite3.DatabaseError) as exc:
        sqlite = {}
        warnings.append(f"Codex SQLite image unavailable: {type(exc).__name__}: {exc}")
    result = []
    for native_id in sorted(indexed.keys() | sqlite.keys()):
        index_value = indexed.get(native_id)
        sqlite_value = sqlite.get(native_id)
        if index_value is not None and index_value[0].strip():
            title, timestamp = index_value
            result.append(
                CodexTitle(
                    native_id, sqlite_value[0] if sqlite_value else "", title.strip()[:500], "session_index", timestamp
                )
            )
        elif sqlite_value is not None:
            result.append(CodexTitle(native_id, sqlite_value[0], sqlite_value[1], sqlite_value[2], None))
    return result, warnings, retry_errors
