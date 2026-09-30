"""On-disk session archive: `index.jsonl`, `manifest.jsonl`, `events/<session_id>.jsonl.gz` (layout: README.md)."""

import asyncio
import gzip
import logging
from collections.abc import Collection
from pathlib import Path

from pydantic import BaseModel

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.models import Event, SessionSummary, canonical_id
from devinfra.claude.session_export.progress import Progress

logger = logging.getLogger(__name__)


class ManifestRecord(BaseModel):
    session_id: str
    last_event_at: str
    event_count: int
    newest_sequence_num: int
    gz_bytes: int

    @property
    def is_complete(self) -> bool:
        """False when the session gained events between paging and the newest-sequence probe."""
        return self.newest_sequence_num == self.event_count


def read_manifest(path: Path) -> dict[str, ManifestRecord]:
    if not path.exists():
        return {}
    records = (ManifestRecord.model_validate_json(line) for line in path.read_text().splitlines())
    return {r.session_id: r for r in records}  # a later record supersedes an earlier one


def sessions_to_export(
    sessions: Collection[SessionSummary], manifest: dict[str, ManifestRecord]
) -> list[SessionSummary]:
    """Sessions never exported, exported while live, or changed since."""
    return [
        s
        for s in sessions
        if not (r := manifest.get(canonical_id(s.id))) or not r.is_complete or r.last_event_at != s.last_event_at
    ]


async def export_session(api: SessionsApi, session: SessionSummary, events_dir: Path) -> ManifestRecord:
    session_id = canonical_id(session.id)
    target = events_dir / f"{session_id}.jsonl.gz"
    partial = target.with_name(f"{target.name}.part")
    progress = Progress(session_id, await api.newest_sequence_num(session_id))
    count = 0
    with gzip.open(partial, "wt", encoding="utf-8") as out:
        async for page in api.iter_event_pages(session_id):
            await asyncio.to_thread(out.write, "".join(f"{e.model_dump_json(exclude_unset=True)}\n" for e in page))
            count += len(page)
            progress.report(count)
    partial.replace(target)
    return ManifestRecord(
        session_id=session_id,
        last_event_at=session.last_event_at,
        event_count=count,
        # A desc query is independent of the asc paging above, so it catches a paging bug as well as a live session.
        newest_sequence_num=await api.newest_sequence_num(session_id),
        gz_bytes=target.stat().st_size,
    )


async def export_all(
    api: SessionsApi, out_dir: Path, *, workers: int, session_ids: Collection[str] | None, limit: int | None
) -> None:
    sessions = [s async for s in api.list_sessions()]
    logger.info("listed %d sessions", len(sessions))
    events_dir = out_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.jsonl").write_text("".join(f"{s.model_dump_json()}\n" for s in sessions))
    if session_ids is not None:
        wanted = {canonical_id(i) for i in session_ids}
        sessions = [s for s in sessions if canonical_id(s.id) in wanted]
    sessions = sessions[:limit]

    manifest_path = out_dir / "manifest.jsonl"
    todo = sessions_to_export(sessions, read_manifest(manifest_path))
    logger.info("%d to export, %d already up to date", len(todo), len(sessions) - len(todo))
    slots = asyncio.Semaphore(workers)
    finished = 0

    async def export_one(session: SessionSummary) -> None:
        nonlocal finished
        async with slots:
            record = await export_session(api, session, events_dir)
        with manifest_path.open("a") as manifest:
            manifest.write(f"{record.model_dump_json()}\n")
        finished += 1
        logger.info(
            "[%d/%d] %s events=%d gz=%dKB",
            finished,
            len(todo),
            record.session_id,
            record.event_count,
            record.gz_bytes // 1024,
        )

    async with asyncio.TaskGroup() as tasks:  # the first failure cancels the rest; a rerun resumes
        for session in todo:
            tasks.create_task(export_one(session))


def verify_archive(out_dir: Path) -> None:
    """Re-read every file: `sequence_num` runs 1..N without gaps and N matches the manifest."""
    index = [SessionSummary.model_validate_json(line) for line in (out_dir / "index.jsonl").read_text().splitlines()]
    manifest = read_manifest(out_dir / "manifest.jsonl")
    problems: list[str] = []
    total_events = total_bytes = 0
    for session in index:
        session_id = canonical_id(session.id)
        if (record := manifest.get(session_id)) is None:
            problems.append(f"{session_id}: not exported")
            continue
        lines = in_sequence = 0
        with gzip.open(out_dir / "events" / f"{session_id}.jsonl.gz", "rt", encoding="utf-8") as f:
            for lines, line in enumerate(f, 1):
                in_sequence += Event.model_validate_json(line).seq == lines
        if not (in_sequence == lines == record.event_count):
            problems.append(f"{session_id}: {in_sequence=} {lines=} manifest={record.event_count}")
        if not record.is_complete:
            problems.append(
                f"{session_id}: live during export, rerun export to catch up ({record.newest_sequence_num=})"
            )
        total_events += lines
        total_bytes += record.gz_bytes
    logger.info(
        "verified %d sessions, %d events, %.1f MB gz, %d problems",
        len(index),
        total_events,
        total_bytes / 1e6,
        len(problems),
    )
    if problems:
        raise ValueError("\n".join(problems))
